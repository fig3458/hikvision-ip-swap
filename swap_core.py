# AIGC START
"""Hikvision camera IP swap via temp IP + multi-NVR channel sync (ISAPI)."""
from __future__ import annotations

import csv
import io
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import (
    HTTPBasicAuthHandler,
    HTTPDigestAuthHandler,
    HTTPPasswordMgrWithDefaultRealm,
    Request,
    build_opener,
)
from xml.sax.saxutils import escape as xml_escape

IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

LogFn = Callable[[str], None]


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class LiveChannel:
    channel_id: int
    ip: str
    name: str
    protocol: str = ""
    manage_port: str = ""
    src_input_port: str = ""
    nvr_url: str = ""
    nvr_label: str = ""

    def channel_label(self) -> str:
        nvr = self.nvr_label or self.nvr_url or "-"
        return f"{nvr}/D{self.channel_id}"


@dataclass
class NvrOptions:
    base_url: str
    nvr_user: str
    nvr_password: str
    cam_user: str = "admin"
    cam_password: str = ""
    protocol: str = "HIKVISION"
    cam_port: int = 8000
    timeout: float = 25.0
    src_input_port: int = 1
    settle_seconds: float = 3.0
    label: str = ""


@dataclass
class CamOptions:
    user: str = "admin"
    password: str = ""
    timeout: float = 8.0
    http_port: int = 80


@dataclass
class ParseNvrResult:
    items: list[NvrOptions]
    skipped: list[str]
    warnings: list[str]


@dataclass
class Occupancy:
    icmp_alive: bool = False
    http_alive: bool = False
    http_detail: str = ""
    nvr_channels: list[LiveChannel] = field(default_factory=list)

    @property
    def occupied(self) -> bool:
        return self.icmp_alive or self.http_alive or bool(self.nvr_channels)

    def summary(self) -> str:
        parts: list[str] = []
        if self.icmp_alive:
            parts.append("ping通")
        if self.http_alive:
            parts.append(f"HTTP在线({self.http_detail or 'deviceInfo'})")
        if self.nvr_channels:
            parts.append(
                "录像机通道："
                + "、".join(c.channel_label() for c in self.nvr_channels)
            )
        return "；".join(parts) if parts else "空闲"


@dataclass
class SwapStep:
    kind: str  # camera_ip | nvr_channel | note | check
    title: str
    detail: str = ""
    from_ip: str = ""
    to_ip: str = ""
    channel: LiveChannel | None = None


@dataclass
class SwapPlan:
    source_ip: str
    target_ip: str
    temp_ip: str
    target_occupied: bool
    source_channels: list[LiveChannel]
    target_channels: list[LiveChannel]
    temp_occupancy: Occupancy
    source_occupancy: Occupancy
    target_occupancy: Occupancy
    steps: list[SwapStep]
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class SwapResult:
    ok: bool
    completed_steps: int
    total_steps: int
    error: str = ""
    dry_run: bool = False


@dataclass
class NvrFetchResult:
    opt: NvrOptions
    ok: bool
    device_name: str = ""
    channels: list[LiveChannel] = field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def normalize_base_url(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("录像机地址不能为空")
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    if not parsed.hostname:
        raise ValueError("录像机地址无效")
    base = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        base += f":{parsed.port}"
    return base


def nvr_ip_label(base_url: str) -> str:
    host = urlparse(base_url).hostname if "://" in (base_url or "") else (base_url or "")
    host = (host or "").strip()
    if IP_RE.fullmatch(host):
        return f"{host.rsplit('.', 1)[-1]}录像机"
    return host or base_url or "录像机"


def require_ip(ip: str, label: str = "IP") -> str:
    ip = (ip or "").strip()
    if not IP_RE.fullmatch(ip):
        raise ValueError(f"{label} 无效：{ip}")
    parts = [int(p) for p in ip.split(".")]
    if any(p < 0 or p > 255 for p in parts):
        raise ValueError(f"{label} 无效：{ip}")
    return ip


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _find_text(el: ET.Element, names: set[str]) -> str:
    want = {n.lower() for n in names}
    for node in el.iter():
        if _local(node.tag).lower() in want and node.text and node.text.strip():
            return node.text.strip()
    return ""


def _child_text(el: ET.Element, name: str) -> str:
    want = name.lower()
    for child in list(el):
        if _local(child.tag).lower() == want and child.text and child.text.strip():
            return child.text.strip()
    return ""


def _split_row(line: str) -> list[str]:
    if "\t" in line:
        return [c.strip() for c in line.split("\t")]
    if "," in line and line.count(",") >= 1:
        try:
            row = next(csv.reader(io.StringIO(line)))
            return [c.strip() for c in row]
        except csv.Error:
            pass
    return [c.strip() for c in re.split(r"\s{2,}", line)]


def parse_response_status(xml_text: str) -> tuple[str, str]:
    if not xml_text or "<" not in xml_text:
        return "", xml_text.strip()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return "", xml_text.strip()[:300]
    code = _find_text(root, {"statusCode", "statuscode"})
    msg = _find_text(root, {"statusString", "subStatusCode", "statusString"})
    extra = _find_text(root, {"subStatusCode"})
    if extra and extra not in msg:
        msg = f"{msg} {extra}".strip()
    return code, msg or xml_text.strip()[:200]


def is_ok_status(code: str, http_code: int) -> bool:
    if http_code >= 400:
        return False
    if not code:
        return http_code < 400
    return code in {"1", "OK", "ok"}


# ---------------------------------------------------------------------------
# NVR list / inventory
# ---------------------------------------------------------------------------


def parse_nvr_list(
    text: str,
    default_user: str,
    default_password: str,
    cam_user: str = "admin",
    cam_password: str = "",
    timeout: float = 25.0,
    protocol: str = "HIKVISION",
    cam_port: int = 8000,
    settle_seconds: float = 3.0,
) -> ParseNvrResult:
    items: list[NvrOptions] = []
    skipped: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        low = line.lower()
        if low.startswith("地址") or low.startswith("url") or low.startswith("录像机"):
            continue

        cols = [c for c in _split_row(line) if c != ""]
        if not cols:
            continue

        addr = cols[0]
        try:
            base = normalize_base_url(addr)
        except ValueError:
            skipped.append(f"地址无效，跳过：{line[:80]}")
            continue

        user = default_user
        password = default_password
        label = ""

        if len(cols) == 2:
            if "@" in cols[1] or cols[1].lower() in {"admin", "user"}:
                user = cols[1]
            else:
                label = cols[1]
        elif len(cols) == 3:
            user, password = cols[1], cols[2]
        elif len(cols) >= 4:
            user, password, label = cols[1], cols[2], cols[3]

        if not user or not password:
            skipped.append(f"缺用户名/密码，跳过：{base}")
            continue
        if base in seen:
            warnings.append(f"录像机地址重复，已忽略：{base}")
            continue
        seen.add(base)
        if not label:
            label = nvr_ip_label(base)
        items.append(
            NvrOptions(
                base_url=base,
                nvr_user=user,
                nvr_password=password,
                cam_user=cam_user,
                cam_password=cam_password,
                protocol=protocol,
                cam_port=cam_port,
                timeout=timeout,
                settle_seconds=settle_seconds,
                label=label,
            )
        )

    return ParseNvrResult(items=items, skipped=skipped, warnings=warnings)


def parse_channel_list(
    xml_text: str,
    nvr_url: str = "",
    nvr_label: str = "",
) -> dict[int, LiveChannel]:
    root = ET.fromstring(xml_text)
    channels: dict[int, LiveChannel] = {}
    nodes = [root] if _local(root.tag).lower() == "inputproxychannel" else []
    if not nodes:
        nodes = [
            el
            for el in root.iter()
            if _local(el.tag).lower() == "inputproxychannel"
        ]
    for el in nodes:
        cid_text = _find_text(el, {"id"})
        if not cid_text.isdigit():
            continue
        cid = int(cid_text)
        ip = _find_text(el, {"ipaddress", "ipv4address", "hostName", "hostname"})
        name = _child_text(el, "name") or _find_text(el, {"name"})
        protocol = _find_text(el, {"proxyprotocol", "protocol"})
        manage_port = _find_text(el, {"manageportno", "manageport"})
        src_input = _find_text(el, {"srcinputport"})
        channels[cid] = LiveChannel(
            channel_id=cid,
            ip=ip,
            name=name,
            protocol=protocol,
            manage_port=manage_port,
            src_input_port=src_input,
            nvr_url=nvr_url,
            nvr_label=nvr_label,
        )
    return channels


# ---------------------------------------------------------------------------
# Occupancy probes
# ---------------------------------------------------------------------------


def icmp_ping(ip: str, timeout_sec: float = 1.5) -> bool:
    """Windows: ping -n 1 -w ms. Returns True if host replies."""
    ms = max(200, int(timeout_sec * 1000))
    try:
        completed = subprocess.run(
            ["ping", "-n", "1", "-w", str(ms), ip],
            capture_output=True,
            text=True,
            timeout=timeout_sec + 2.0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    out = (completed.stdout or "") + (completed.stderr or "")
    low = out.lower()
    if completed.returncode == 0 and ("ttl=" in low or "ttl =" in low):
        return True
    # Chinese Windows success often still has TTL=
    if "TTL=" in out or "ttl=" in low:
        return True
    return False


def http_probe_device(
    ip: str,
    user: str = "",
    password: str = "",
    http_port: int = 80,
    timeout: float = 3.0,
) -> tuple[bool, str]:
    """Try GET /ISAPI/System/deviceInfo. Auth optional for reachability."""
    if http_port == 80:
        base = f"http://{ip}"
    else:
        base = f"http://{ip}:{http_port}"
    url = base + "/ISAPI/System/deviceInfo"
    headers = {
        "Accept": "application/xml, text/xml, */*",
        "User-Agent": "HikvisionIpSwap/1.0",
    }
    opener = build_opener()
    if user:
        passman = HTTPPasswordMgrWithDefaultRealm()
        passman.add_password(None, base, user, password or "")
        opener = build_opener(
            HTTPDigestAuthHandler(passman),
            HTTPBasicAuthHandler(passman),
        )
    req = Request(url, headers=headers, method="GET")
    try:
        with opener.open(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            code = getattr(resp, "status", 200) or 200
    except HTTPError as exc:
        # 401 means device is alive
        if exc.code in {401, 403}:
            return True, f"HTTP {exc.code}"
        text = exc.read().decode("utf-8", errors="replace")
        if exc.code < 500:
            return True, f"HTTP {exc.code}"
        return False, f"HTTP {exc.code} {text[:80]}"
    except (URLError, TimeoutError, OSError) as exc:
        return False, str(exc)
    detail = ""
    try:
        root = ET.fromstring(text)
        detail = _find_text(root, {"deviceName", "model", "serialNumber"})
    except ET.ParseError:
        detail = "deviceInfo"
    return code < 500, detail or "OK"


def find_channels_by_ip(
    channels: list[LiveChannel], ip: str
) -> list[LiveChannel]:
    return [c for c in channels if c.ip == ip]


def probe_occupancy(
    ip: str,
    all_channels: list[LiveChannel],
    cam_user: str = "",
    cam_password: str = "",
    http_port: int = 80,
    timeout: float = 3.0,
) -> Occupancy:
    icmp = icmp_ping(ip, timeout_sec=min(2.0, timeout))
    http_ok, http_detail = http_probe_device(
        ip, user=cam_user, password=cam_password, http_port=http_port, timeout=timeout
    )
    # If auth required and we got 401 without credentials, still occupied
    if not http_ok and cam_user:
        http_ok2, http_detail2 = http_probe_device(
            ip, user="", password="", http_port=http_port, timeout=timeout
        )
        if http_ok2:
            http_ok, http_detail = http_ok2, http_detail2
    return Occupancy(
        icmp_alive=icmp,
        http_alive=http_ok,
        http_detail=http_detail,
        nvr_channels=find_channels_by_ip(all_channels, ip),
    )


# ---------------------------------------------------------------------------
# Camera client
# ---------------------------------------------------------------------------


class CameraClient:
    def __init__(self, ip: str, opt: CamOptions, http_port: int | None = None):
        self.ip = ip
        self.opt = opt
        self.http_port = http_port if http_port is not None else opt.http_port
        if self.http_port == 80:
            self.base_url = f"http://{ip}"
        else:
            self.base_url = f"http://{ip}:{self.http_port}"
        passman = HTTPPasswordMgrWithDefaultRealm()
        passman.add_password(None, self.base_url, opt.user, opt.password)
        self.opener = build_opener(
            HTTPDigestAuthHandler(passman),
            HTTPBasicAuthHandler(passman),
        )

    def request(
        self, method: str, path: str, body: str | None = None
    ) -> tuple[int, str]:
        url = self.base_url + path
        data = body.encode("utf-8") if body is not None else None
        headers = {
            "Accept": "application/xml, text/xml, */*",
            "User-Agent": "HikvisionIpSwap/1.0",
        }
        if body is not None:
            headers["Content-Type"] = 'application/xml; charset="UTF-8"'
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=self.opt.timeout) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                return getattr(resp, "status", 200) or 200, text
        except HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            return exc.code, text
        except URLError as exc:
            raise ConnectionError(
                str(exc.reason if hasattr(exc, "reason") else exc)
            ) from exc

    def ping(self) -> str:
        code, text = self.request("GET", "/ISAPI/System/deviceInfo")
        if code == 401:
            raise RuntimeError("摄像头账号或密码错误（HTTP 401）")
        if code >= 400:
            raise RuntimeError(f"登录摄像头失败 HTTP {code}：{text[:200]}")
        try:
            root = ET.fromstring(text)
            name = _find_text(root, {"deviceName", "model"})
        except ET.ParseError:
            name = ""
        return name or "OK"

    def list_interface_ids(self) -> list[str]:
        code, text = self.request("GET", "/ISAPI/System/Network/interfaces")
        if code >= 400:
            # fall back to interface 1
            return ["1"]
        ids: list[str] = []
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return ["1"]
        for el in root.iter():
            if _local(el.tag).lower() != "networkinterface":
                continue
            iid = _child_text(el, "id") or _find_text(el, {"id"})
            if iid:
                ids.append(iid)
        return ids or ["1"]

    def get_ip_address_xml(self, interface_id: str = "1") -> str:
        path = f"/ISAPI/System/Network/interfaces/{interface_id}/ipAddress"
        code, text = self.request("GET", path)
        if code >= 400:
            raise RuntimeError(
                f"读取网卡 {interface_id} IP 失败 HTTP {code}：{text[:200]}"
            )
        return text

    def set_ipv4_address(
        self,
        new_ip: str,
        interface_id: str | None = None,
        log: LogFn | None = None,
    ) -> None:
        """Read current IPAddress XML, replace IPv4, PUT back (keep mask/gateway/DNS)."""
        log = log or (lambda _m: None)
        ids = [interface_id] if interface_id else self.list_interface_ids()
        last_err = ""
        for iid in ids:
            try:
                xml = self.get_ip_address_xml(iid)
            except RuntimeError as exc:
                last_err = str(exc)
                continue
            if not re.search(r"<ipAddress>[^<]*</ipAddress>", xml, flags=re.I):
                # sometimes nested under different casing / only one top-level
                last_err = f"接口 {iid} 响应里找不到 <ipAddress>"
                continue
            # Prefer replacing the primary device address (first ipAddress that looks like IPv4 host,
            # not nested gateway if structure differs — Hikvision uses top-level <ipAddress>).
            new_xml, n = re.subn(
                r"(<ipAddress>)(\d{1,3}(?:\.\d{1,3}){3})(</ipAddress>)",
                rf"\g<1>{new_ip}\g<3>",
                xml,
                count=1,
                flags=re.I,
            )
            if n == 0:
                new_xml, n = re.subn(
                    r"<ipAddress>[^<]*</ipAddress>",
                    f"<ipAddress>{xml_escape(new_ip)}</ipAddress>",
                    xml,
                    count=1,
                    flags=re.I,
                )
            if n == 0:
                last_err = f"接口 {iid} 无法替换 ipAddress"
                continue
            # Force static if addressingType present
            new_xml = re.sub(
                r"<addressingType>[^<]*</addressingType>",
                "<addressingType>static</addressingType>",
                new_xml,
                count=1,
                flags=re.I,
            )
            path = f"/ISAPI/System/Network/interfaces/{iid}/ipAddress"
            log(f"  PUT {self.ip}{path} → {new_ip}")
            code, text = self.request("PUT", path, new_xml)
            sc, msg = parse_response_status(text)
            if code == 401:
                raise RuntimeError("摄像头账号或密码错误（改 IP 被拒 HTTP 401）")
            if is_ok_status(sc, code):
                return
            last_err = f"HTTP {code} {sc} {msg}".strip()
        raise RuntimeError(f"修改摄像头 IP 失败：{last_err}")


def wait_camera_reachable(
    ip: str,
    opt: CamOptions,
    timeout: float = 45.0,
    log: LogFn | None = None,
) -> str:
    log = log or (lambda _m: None)
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            name = CameraClient(ip, opt).ping()
            return name
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
            time.sleep(1.2)
    raise RuntimeError(f"等待摄像头 {ip} 可达超时：{last}")


def wait_camera_gone(
    ip: str,
    opt: CamOptions,
    timeout: float = 20.0,
    log: LogFn | None = None,
) -> None:
    log = log or (lambda _m: None)
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive_http, _ = http_probe_device(
            ip,
            user=opt.user,
            password=opt.password,
            http_port=opt.http_port,
            timeout=min(2.0, opt.timeout),
        )
        if not alive_http and not icmp_ping(ip, timeout_sec=1.0):
            return
        time.sleep(1.0)
    log(f"  警告：旧地址 {ip} 在超时后仍可能在线（继续）")


def change_camera_ip(
    old_ip: str,
    new_ip: str,
    opt: CamOptions,
    log: LogFn | None = None,
    wait_timeout: float = 45.0,
) -> None:
    log = log or (lambda _m: None)
    client = CameraClient(old_ip, opt)
    info = client.ping()
    log(f"  摄像头 {old_ip} 已登录：{info}")
    client.set_ipv4_address(new_ip, log=log)
    log(f"  已下发改 IP：{old_ip} → {new_ip}，等待生效…")
    wait_camera_gone(old_ip, opt, timeout=min(15.0, wait_timeout / 2), log=log)
    name = wait_camera_reachable(new_ip, opt, timeout=wait_timeout, log=log)
    log(f"  新地址 {new_ip} 可达：{name}")


# ---------------------------------------------------------------------------
# NVR client
# ---------------------------------------------------------------------------


class NvrClient:
    def __init__(self, opt: NvrOptions):
        self.opt = opt
        self.base_url = opt.base_url.rstrip("/")
        passman = HTTPPasswordMgrWithDefaultRealm()
        passman.add_password(None, self.base_url, opt.nvr_user, opt.nvr_password)
        self.opener = build_opener(
            HTTPDigestAuthHandler(passman),
            HTTPBasicAuthHandler(passman),
        )

    def request(
        self, method: str, path: str, body: str | None = None
    ) -> tuple[int, str]:
        url = self.base_url + path
        data = body.encode("utf-8") if body is not None else None
        headers = {
            "Accept": "application/xml, text/xml, */*",
            "User-Agent": "HikvisionIpSwap/1.0",
        }
        if body is not None:
            headers["Content-Type"] = 'application/xml; charset="UTF-8"'
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=self.opt.timeout) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                return getattr(resp, "status", 200) or 200, text
        except HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            return exc.code, text
        except URLError as exc:
            raise ConnectionError(
                str(exc.reason if hasattr(exc, "reason") else exc)
            ) from exc

    def ping(self) -> str:
        code, text = self.request("GET", "/ISAPI/System/deviceInfo")
        if code >= 400:
            raise RuntimeError(f"登录录像机失败 HTTP {code}：{text[:200]}")
        try:
            root = ET.fromstring(text)
            name = _find_text(root, {"deviceName", "model"})
        except ET.ParseError:
            name = ""
        return name or "OK"

    def list_channels(self) -> dict[int, LiveChannel]:
        code, text = self.request("GET", "/ISAPI/ContentMgmt/InputProxy/channels")
        if code >= 400:
            raise RuntimeError(f"读取通道列表失败 HTTP {code}：{text[:200]}")
        label = self.opt.label or nvr_ip_label(self.base_url)
        return parse_channel_list(text, nvr_url=self.base_url, nvr_label=label)

    def wait_ip_absent(self, ip: str, timeout: float | None = None) -> dict[int, LiveChannel]:
        timeout = timeout if timeout is not None else max(8.0, self.opt.settle_seconds * 3)
        time.sleep(self.opt.settle_seconds)
        deadline = time.time() + timeout
        last: dict[int, LiveChannel] = {}
        while True:
            last = self.list_channels()
            if ip not in {ch.ip for ch in last.values()}:
                time.sleep(0.5)
                return last
            if time.time() >= deadline:
                leftover = [f"D{cid}" for cid, ch in last.items() if ch.ip == ip]
                raise RuntimeError(f"等待释放超时，{ip} 仍在 {', '.join(leftover)}")
            time.sleep(1.0)

    def wait_ip_present(self, ip: str, timeout: float | None = None) -> dict[int, LiveChannel]:
        timeout = timeout if timeout is not None else max(8.0, self.opt.settle_seconds * 3)
        deadline = time.time() + timeout
        last: dict[int, LiveChannel] = {}
        while True:
            last = self.list_channels()
            if any(ch.ip == ip for ch in last.values()):
                return last
            if time.time() >= deadline:
                return last
            time.sleep(0.8)

    def find_channels_by_ip(
        self, ip: str, channels: dict[int, LiveChannel] | None = None
    ) -> list[int]:
        mapping = channels if channels is not None else self.list_channels()
        return sorted(cid for cid, ch in mapping.items() if ch.ip == ip)

    def update_channel_ip(
        self,
        channel_id: int,
        new_ip: str,
        name: str = "",
        log: LogFn | None = None,
    ) -> None:
        """Only in-place PUT of <ipAddress>. Never delete/add or change channel number."""
        del name  # kept for call-site compatibility; channel name is preserved in XML
        log = log or (lambda _m: None)
        path = f"/ISAPI/ContentMgmt/InputProxy/channels/{channel_id}"
        code, text = self.request("GET", path)
        if code >= 400:
            raise RuntimeError(f"读取 D{channel_id} 失败 HTTP {code}：{text[:200]}")
        if not re.search(r"<ipAddress>[^<]*</ipAddress>", text, flags=re.I):
            raise RuntimeError(
                f"D{channel_id} 响应里找不到 <ipAddress>，拒绝删加通道；请手工在录像机里改 IP"
            )

        new_xml = re.sub(
            r"<ipAddress>[^<]*</ipAddress>",
            f"<ipAddress>{xml_escape(new_ip)}</ipAddress>",
            text,
            count=1,
            flags=re.I,
        )
        if self.opt.cam_password and re.search(
            r"<password>[^<]*</password>", new_xml, flags=re.I
        ):
            new_xml = re.sub(
                r"<password>[^<]*</password>",
                f"<password>{xml_escape(self.opt.cam_password)}</password>",
                new_xml,
                count=1,
                flags=re.I,
            )

        log(f"  PUT 通道 D{channel_id} 内 IP → {new_ip}（不改通道号）")
        code2, text2 = self.request("PUT", path, new_xml)
        sc, msg = parse_response_status(text2)
        if not is_ok_status(sc, code2):
            raise RuntimeError(
                f"就地改 D{channel_id} 的 IP 失败 HTTP {code2} {sc} {msg}。"
                "已按「只改 IP、不删通道」策略停止，请手工处理".strip()
            )

        time.sleep(self.opt.settle_seconds)
        ch = self.list_channels().get(channel_id)
        if ch and ch.ip == new_ip:
            return
        present = self.wait_ip_present(new_ip, timeout=8.0)
        if channel_id in present and present[channel_id].ip == new_ip:
            return
        raise RuntimeError(
            f"D{channel_id} PUT 已返回成功，但校验仍未看到 IP={new_ip}；"
            "通道号未改动，请刷新录像机确认"
        )


def _fetch_one_nvr(opt: NvrOptions) -> NvrFetchResult:
    try:
        client = NvrClient(opt)
        name = client.ping()
        channels = client.list_channels()
        label = opt.label or nvr_ip_label(opt.base_url)
        for ch in channels.values():
            ch.nvr_label = label
            ch.nvr_url = opt.base_url
        return NvrFetchResult(
            opt=opt, ok=True, device_name=name, channels=list(channels.values())
        )
    except Exception as exc:  # noqa: BLE001
        return NvrFetchResult(opt=opt, ok=False, error=str(exc))


def fetch_all_channels(
    targets: list[NvrOptions],
    on_log: LogFn | None = None,
    max_workers: int = 6,
) -> tuple[list[LiveChannel], list[NvrFetchResult]]:
    log = on_log or (lambda _m: None)
    if not targets:
        raise ValueError("没有可拉取的录像机")
    results: list[NvrFetchResult] = []
    workers = max(1, min(max_workers, len(targets)))
    log(f"开始拉取 {len(targets)} 台录像机通道（并行 {workers}）…")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_fetch_one_nvr, t): t for t in targets}
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            label = r.opt.label or r.opt.base_url
            if r.ok:
                log(f"  ✓ {label}：{len(r.channels)} 路（{r.device_name}）")
            else:
                log(f"  ✗ {label}：{r.error}")
    # stable order by nvr list
    order = {t.base_url: i for i, t in enumerate(targets)}
    results.sort(key=lambda r: order.get(r.opt.base_url, 999))
    channels: list[LiveChannel] = []
    for r in results:
        if r.ok:
            channels.extend(r.channels)
    return channels, results


def nvr_client_for_channel(
    ch: LiveChannel, targets: list[NvrOptions]
) -> NvrClient:
    for t in targets:
        if t.base_url.rstrip("/") == (ch.nvr_url or "").rstrip("/"):
            return NvrClient(t)
    raise RuntimeError(f"找不到通道 {ch.channel_label()} 对应的录像机配置")


# ---------------------------------------------------------------------------
# Planning & orchestration
# ---------------------------------------------------------------------------


def build_swap_plan(
    source_ip: str,
    target_ip: str,
    temp_ip: str,
    targets: list[NvrOptions],
    cam_opt: CamOptions,
    on_log: LogFn | None = None,
) -> SwapPlan:
    log = on_log or (lambda _m: None)
    source_ip = require_ip(source_ip, "源 IP")
    target_ip = require_ip(target_ip, "目标 IP")
    temp_ip = require_ip(temp_ip, "临时 IP")

    if len({source_ip, target_ip, temp_ip}) < 3:
        raise ValueError("源 IP、目标 IP、临时 IP 必须两两不同")

    channels, fetch_results = fetch_all_channels(targets, on_log=log)
    failed = [r for r in fetch_results if not r.ok]
    warnings: list[str] = []
    notes: list[str] = []
    if failed:
        warnings.append(
            f"{len(failed)} 台录像机拉取失败："
            + "、".join(r.opt.label or r.opt.base_url for r in failed)
        )

    log("探测源 / 目标 / 临时 IP 占用…")
    source_occ = probe_occupancy(
        source_ip, channels, cam_opt.user, cam_opt.password, cam_opt.http_port
    )
    target_occ = probe_occupancy(
        target_ip, channels, cam_opt.user, cam_opt.password, cam_opt.http_port
    )
    temp_occ = probe_occupancy(
        temp_ip, channels, cam_opt.user, cam_opt.password, cam_opt.http_port
    )
    log(f"  源 {source_ip}：{source_occ.summary()}")
    log(f"  目标 {target_ip}：{target_occ.summary()}")
    log(f"  临时 {temp_ip}：{temp_occ.summary()}")

    if temp_occ.occupied:
        raise RuntimeError(
            f"临时 IP {temp_ip} 已被占用（{temp_occ.summary()}），请换一个空闲地址"
        )

    if not source_occ.http_alive and not source_occ.icmp_alive:
        # still allow if only on NVR? No — must change camera IP, need login
        raise RuntimeError(
            f"源摄像头 {source_ip} 探测不到（ping/HTTP 均失败），无法改 IP"
        )

    # verify camera login early
    try:
        CameraClient(source_ip, cam_opt).ping()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"源摄像头 {source_ip} 登录失败：{exc}") from exc

    target_occupied = target_occ.http_alive or target_occ.icmp_alive
    if target_occ.nvr_channels and not target_occupied:
        raise RuntimeError(
            f"目标 {target_ip} 设备探测不到，但仍登记在录像机通道："
            + "、".join(c.channel_label() for c in target_occ.nvr_channels)
            + "。本工具只改通道内 IP、不删不挪通道号；请先确认设备或手工处理该通道后再试"
        )
    if target_occupied:
        try:
            CameraClient(target_ip, cam_opt).ping()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"目标 {target_ip} 在线但登录失败，无法中转：{exc}"
            ) from exc

    source_channels = list(source_occ.nvr_channels)
    target_channels = list(target_occ.nvr_channels)
    if not source_channels:
        notes.append(f"源 {source_ip} 未出现在任何录像机通道中，将只改摄像头 IP")
    if target_occupied and not target_channels:
        notes.append(f"占用方 {target_ip} 未在录像机中，将只改摄像头 IP")

    steps: list[SwapStep] = []
    steps.append(
        SwapStep(
            kind="check",
            title="校验临时 IP 空闲",
            detail=f"{temp_ip} 空闲",
        )
    )

    if target_occupied:
        steps.append(
            SwapStep(
                kind="camera_ip",
                title="占用方摄像头改到临时 IP",
                from_ip=target_ip,
                to_ip=temp_ip,
                detail=f"{target_ip} → {temp_ip}",
            )
        )
        for ch in target_channels:
            steps.append(
                SwapStep(
                    kind="nvr_channel",
                    title=f"录像机通道改 IP（{ch.channel_label()}）",
                    from_ip=target_ip,
                    to_ip=temp_ip,
                    channel=ch,
                    detail=f"{ch.channel_label()}：{target_ip} → {temp_ip}",
                )
            )

    steps.append(
        SwapStep(
            kind="camera_ip",
            title="源摄像头改到目标 IP",
            from_ip=source_ip,
            to_ip=target_ip,
            detail=f"{source_ip} → {target_ip}",
        )
    )
    for ch in source_channels:
        steps.append(
            SwapStep(
                kind="nvr_channel",
                title=f"录像机通道改 IP（{ch.channel_label()}）",
                from_ip=source_ip,
                to_ip=target_ip,
                channel=ch,
                detail=f"{ch.channel_label()}：{source_ip} → {target_ip}",
            )
        )

    if target_occupied:
        steps.append(
            SwapStep(
                kind="camera_ip",
                title="占用方从临时 IP 改回源旧地址（完成对调）",
                from_ip=temp_ip,
                to_ip=source_ip,
                detail=f"{temp_ip} → {source_ip}",
            )
        )
        for ch in target_channels:
            steps.append(
                SwapStep(
                    kind="nvr_channel",
                    title=f"录像机通道改 IP（{ch.channel_label()}）",
                    from_ip=temp_ip,
                    to_ip=source_ip,
                    channel=ch,
                    detail=f"{ch.channel_label()}：{temp_ip} → {source_ip}",
                )
            )

    return SwapPlan(
        source_ip=source_ip,
        target_ip=target_ip,
        temp_ip=temp_ip,
        target_occupied=target_occupied,
        source_channels=source_channels,
        target_channels=target_channels,
        temp_occupancy=temp_occ,
        source_occupancy=source_occ,
        target_occupancy=target_occ,
        steps=steps,
        warnings=warnings,
        notes=notes,
    )


def format_plan(plan: SwapPlan) -> str:
    lines = [
        f"对调计划：{plan.source_ip} → {plan.target_ip}",
        f"临时中转：{plan.temp_ip}",
        f"目标占用：{'是（将完整对调）' if plan.target_occupied else '否（直接改源）'}",
        f"源通道："
        + (
            "、".join(c.channel_label() for c in plan.source_channels)
            if plan.source_channels
            else "（未找到）"
        ),
        f"占用方通道："
        + (
            "、".join(c.channel_label() for c in plan.target_channels)
            if plan.target_channels
            else "（无/未找到）"
        ),
        "",
        "步骤：",
    ]
    for i, step in enumerate(plan.steps, 1):
        lines.append(f"  {i}. [{step.kind}] {step.title}")
        if step.detail:
            lines.append(f"      {step.detail}")
    if plan.warnings:
        lines.append("")
        lines.append("警告：")
        for w in plan.warnings:
            lines.append(f"  ! {w}")
    if plan.notes:
        lines.append("")
        lines.append("备注：")
        for n in plan.notes:
            lines.append(f"  - {n}")
    return "\n".join(lines)


def execute_swap(
    plan: SwapPlan,
    targets: list[NvrOptions],
    cam_opt: CamOptions,
    dry_run: bool = True,
    on_log: LogFn | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> SwapResult:
    log = on_log or (lambda _m: None)
    stop = should_stop or (lambda: False)
    total = len(plan.steps)
    completed = 0

    # Re-check temp IP right before write
    if not dry_run:
        channels, _ = fetch_all_channels(targets, on_log=log)
        temp_occ = probe_occupancy(
            plan.temp_ip, channels, cam_opt.user, cam_opt.password, cam_opt.http_port
        )
        if temp_occ.occupied:
            return SwapResult(
                ok=False,
                completed_steps=0,
                total_steps=total,
                error=f"执行前复查：临时 IP {plan.temp_ip} 已被占用（{temp_occ.summary()}）",
                dry_run=False,
            )

    log(format_plan(plan))
    if dry_run:
        log("")
        log("【演练模式】未写入任何设备。取消「只演练」后再执行。")
        return SwapResult(
            ok=True, completed_steps=total, total_steps=total, dry_run=True
        )

    for i, step in enumerate(plan.steps, 1):
        if stop():
            return SwapResult(
                ok=False,
                completed_steps=completed,
                total_steps=total,
                error=f"用户停止（已完成 {completed}/{total} 步）",
            )
        log(f"—— 步骤 {i}/{total}：{step.title}")
        try:
            if step.kind == "check":
                log(f"  {step.detail or 'OK'}")
            elif step.kind == "camera_ip":
                change_camera_ip(step.from_ip, step.to_ip, cam_opt, log=log)
            elif step.kind == "nvr_channel":
                if step.channel is None:
                    raise RuntimeError("步骤缺少通道信息")
                client = nvr_client_for_channel(step.channel, targets)
                client.update_channel_ip(
                    step.channel.channel_id,
                    step.to_ip,
                    name=step.channel.name,
                    log=log,
                )
                log(f"  通道 {step.channel.channel_label()} 内 IP 已改为 {step.to_ip}")
            else:
                log(f"  {step.detail}")
            completed = i
            log(f"  ✓ 完成")
        except Exception as exc:  # noqa: BLE001
            err = (
                f"步骤 {i}/{total} 失败：{step.title}\n"
                f"{exc}\n"
                f"已成功完成 {completed} 步。请按日志手工收尾后重试。"
            )
            log(f"  ✗ {exc}")
            return SwapResult(
                ok=False,
                completed_steps=completed,
                total_steps=total,
                error=err,
                dry_run=False,
            )

    log("")
    log(f"全部完成：{plan.source_ip} ↔ {plan.target_ip}" if plan.target_occupied else f"全部完成：{plan.source_ip} → {plan.target_ip}")
    return SwapResult(
        ok=True, completed_steps=completed, total_steps=total, dry_run=False
    )


# AIGC END
