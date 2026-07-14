import os
import sys
import subprocess
import zipfile
import time
import json
import urllib.request
import urllib.parse
import shutil
import hashlib
import re
import signal
import threading
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).parent
TOOLS_DIR = BASE_DIR / ".tools"
ARIA2C_PATH = TOOLS_DIR / "aria2c.exe"
ARIA2C_URL = "https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip"
API_BASE = "https://api.ipsw.me/v4"
POLL_INTERVAL = 5
STALL_LIMIT = 12
LOG_FILE = BASE_DIR / "download_log.txt"

BOT_TOKEN = None
CHAT_ID = None

results = []
results_lock = threading.Lock()
stop_requested = False
downloaded_urls = set()
url_file_map = {}

IDM_PATHS = [
    Path(r"C:\Program Files (x86)\Internet Download Manager\IDMan.exe"),
    Path(r"C:\Program Files\Internet Download Manager\IDMan.exe"),
]


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    sys.stdout.flush()
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def signal_handler(sig, frame):
    global stop_requested
    if not stop_requested:
        log("\n[!] Ctrl+C detected. Finishing current download...")
        log("[!] Press Ctrl+C again to force quit")
        stop_requested = True
    else:
        log("[!] Force quitting...")
        sys.exit(1)


def find_idm():
    for p in IDM_PATHS:
        if p.exists():
            return p
    return None


def find_external_downloader():
    idm = find_idm()
    if idm:
        return "idm", idm
    return None, None


def is_idm_alive():
    try:
        r = subprocess.run(['tasklist', '/NH', '/FI', 'IMAGENAME eq IDMan.exe'], capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        return 'IDMan.exe' in r.stdout
    except:
        return True


def ensure_aria2():
    if ARIA2C_PATH.exists():
        return True
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = TOOLS_DIR / "aria2.zip"
    log("[*] Downloading aria2c portable (fallback)...")
    urllib.request.urlretrieve(ARIA2C_URL, zip_path)
    with zipfile.ZipFile(zip_path) as z:
        for f in z.namelist():
            if f.endswith("aria2c.exe"):
                with z.open(f) as src, open(ARIA2C_PATH, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                break
    zip_path.unlink()
    os.chmod(ARIA2C_PATH, 0o755)
    log("[+] aria2c ready")
    return True


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "firmware-downloader/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def get_devices():
    return fetch_json(f"{API_BASE}/devices")


def get_device_firmwares(identifier):
    return fetch_json(f"{API_BASE}/device/{identifier}?type=ipsw")


def get_latest_firmware(firmwares):
    if not firmwares:
        return None
    def sort_key(fw):
        val = fw.get("releasedate")
        return val if val else ""
    def is_stable(fw):
        ver = fw.get("version", "").lower()
        bid = fw.get("buildid", "")
        if "beta" in ver or "beta" in bid.lower():
            return False
        if re.search(r'[a-z]', bid):
            parts = bid.split('.')
            for p in parts:
                if re.search(r'[a-z]', p):
                    return False
        return True
    stable = [f for f in firmwares if f.get("signed") == True and is_stable(f)]
    if stable:
        return sorted(stable, key=sort_key, reverse=True)[0]
    signed = [f for f in firmwares if f.get("signed") == True]
    if signed:
        return sorted(signed, key=sort_key, reverse=True)[0]
    return sorted(firmwares, key=sort_key, reverse=True)[0]


def compute_sha1(filepath):
    h = hashlib.sha1()
    total = filepath.stat().st_size
    read_bytes = 0
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
            read_bytes += len(chunk)
            if read_bytes % (50 * 1024 * 1024) == 0:
                pct = min(read_bytes * 100 // total, 100) if total else 0
                sys.stdout.write(f"\r    Verifying SHA1... {pct}%")
                sys.stdout.flush()
    sys.stdout.write(f"\r    Verifying SHA1... 100%\n")
    sys.stdout.flush()
    return h.hexdigest()


def verify_file(filepath, sha1=None, expected_size=None):
    if not filepath.exists():
        return False, "Not found"
    if expected_size and filepath.stat().st_size != expected_size:
        return False, "Size mismatch"
    if sha1:
        actual = compute_sha1(filepath)
        if actual.lower() != sha1.lower():
            return False, "SHA1 mismatch"
    return True, "OK"


def wait_for_idm(filepath, expected_size, url, timeout_minutes=180):
    last_size = 0
    stall_count = 0
    start = time.time()
    launch_retries = 0
    idm = find_idm()
    original_name = url.split("/")[-1]
    original_path = BASE_DIR / original_name

    while time.time() - start < timeout_minutes * 60:
        if stop_requested:
            return False, "cancelled"

        if filepath.exists():
            current = filepath.stat().st_size
            pct = min(current * 100 // expected_size, 100) if expected_size else 0
            sys.stdout.write(f"\r    IDM: {pct}% ({current/1024**3:.1f}/{expected_size/1024**3:.1f} GB)")
            sys.stdout.flush()
            if current >= expected_size:
                print()
                return True, "complete"
            if current == last_size:
                stall_count += 1
            else:
                stall_count = 0
            last_size = current
        else:
            matched = [f for f in BASE_DIR.glob("*.ipsw") if f.stat().st_size == expected_size]
            for f in matched:
                if str(f) != str(filepath):
                    log(f"\n    Found matching file: {f.name} -> {filepath.name}")
                    f.rename(filepath)
                    print()
                    return True, "complete (renamed)"
            tmp_files = list(BASE_DIR.glob(f"{filepath.stem}*")) + list(BASE_DIR.glob(f"{original_path.stem}*"))
            if not tmp_files:
                sys.stdout.write(f"\r    Waiting for IDM to start...")
                sys.stdout.flush()

        if stall_count >= STALL_LIMIT:
            if not is_idm_alive():
                print()
                return False, "IDM closed"
            print()
            if launch_retries < 2 and not filepath.exists():
                launch_retries += 1
                stall_count = 0
                name = filepath.name if launch_retries == 1 else original_name
                p = str(BASE_DIR)
                log(f"\n    Retrying IDM ({launch_retries}/2)...")
                subprocess.run([str(idm), "/d", url, "/p", p, "/f", name, "/n"], capture_output=True, timeout=10)
                start = time.time()
                continue
            return False, f"stalled at {last_size}/{expected_size}"
        time.sleep(POLL_INTERVAL)

    print()
    return False, "timeout"


def download_with_idm(url, output_path, expected_size):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    idm = find_idm()
    cmd = [str(idm), "/d", url, "/p", str(output_path.parent), "/f", output_path.name, "/n"]
    try:
        subprocess.run(cmd, capture_output=True, timeout=10)
    except subprocess.TimeoutExpired:
        pass
    return wait_for_idm(output_path, expected_size, url)


def download_with_aria2(url, output_path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ARIA2C_PATH), "-x", "16", "-s", "16",
        "--continue=true", "--file-allocation=none",
        "--console-log-level=error", "--summary-interval=0",
        "--connect-timeout=15", "--timeout=30",
        "--retry-wait=3", "--max-tries=5",
        "--dir", str(output_path.parent), "--out", output_path.name,
        url
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "errorCode=1" in stderr or "errorCode=3" in stderr:
            return False, "URL unreachable"
        return False, f"aria2 error ({result.returncode})"
    return True, "OK"


def download_device(device):
    global stop_requested
    if stop_requested:
        return None

    identifier = device["identifier"]
    device_name = device["name"]

    try:
        data = get_device_firmwares(identifier)
    except Exception as e:
        return {"identifier": identifier, "name": device_name, "status": "error", "error": f"API: {e}", "size": 0}

    firmwares = data.get("firmwares", [])
    latest = get_latest_firmware(firmwares)

    if not latest:
        return {"identifier": identifier, "name": device_name, "status": "skipped", "error": "No firmwares", "size": 0}

    url = latest["url"]
    version = latest["version"]
    build = latest["buildid"]
    sha1 = latest.get("sha1sum")
    filesize = latest.get("filesize", 0)

    safe_name = re.sub(r'[<>:"/\\|?*()]', '', device_name.replace('(', '').replace(')', '')).strip()
    filename = f"{identifier} ({safe_name})_{version}_{build}_Restore.ipsw"
    output_path = BASE_DIR / filename

    if url in downloaded_urls:
        if url in url_file_map:
            existing_path, devices = url_file_map[url]
            current_clean = re.sub(r'[<>:"/\\|?*()]', '', device_name.replace('(', '').replace(')', '')).strip()
            if current_clean not in [d[1] for d in devices]:
                devices.append((identifier, safe_name))
            all_ids = "+".join(sorted(set(d[0] for d in devices)))
            all_names = " + ".join(d[1] for d in devices)
            new_fn = f"{all_ids} ({all_names})_{version}_{build}_Restore.ipsw"
            new_path = BASE_DIR / new_fn
            if existing_path != new_path and existing_path.exists():
                existing_path.rename(new_path)
            url_file_map[url] = (new_path, devices)
            return {"identifier": identifier, "name": device_name, "version": version, "build": build,
                    "status": "exists", "size": filesize, "filepath": new_path, "sha1": sha1}
        return {"identifier": identifier, "name": device_name, "version": version, "build": build,
                "status": "exists", "size": filesize, "filepath": output_path, "sha1": sha1}

    if output_path.exists() and output_path.stat().st_size == filesize:
        downloaded_urls.add(url)
        url_file_map[url] = (output_path, [(identifier, safe_name)])
        return {"identifier": identifier, "name": device_name, "version": version, "build": build,
                "status": "exists", "size": filesize, "filepath": output_path, "sha1": sha1}

    existing = [f for f in BASE_DIR.glob("*.ipsw") if f.stat().st_size == filesize]
    for f in existing:
        if str(f) != str(output_path):
            old_stem = f.stem
            old_ids_str = old_stem.split(" (")[0] if " (" in old_stem else old_stem
            old_names_str = ""
            if " (" in old_stem and ")" in old_stem:
                old_names_str = old_stem.split(" (")[1].split(")")[0]
            old_ids = [oid for oid in old_ids_str.split("+") if oid and oid != identifier]
            old_names = [n.strip() for n in old_names_str.split("+") if n.strip()] if old_names_str else []
            current_clean = re.sub(r'[<>:"/\\|?*()]', '', device_name.replace('(', '').replace(')', '')).strip()
            if old_ids:
                all_ids_list = sorted(set(old_ids + [identifier]))
                all_names_list = old_names + ([current_clean] if current_clean not in old_names else [])
                all_ids = "+".join(all_ids_list)
                all_names_joined = " + ".join(all_names_list)
                safe = re.sub(r'[<>:"/\\|?*()]', '', all_names_joined.replace('(', '').replace(')', '')).strip()
                new_fn = f"{all_ids} ({safe})_{version}_{build}_Restore.ipsw"
                new_path = BASE_DIR / new_fn
                if str(f) != str(new_path):
                    log(f"  ~> Merging old: {f.name} -> {new_fn}")
                    f.rename(new_path)
                downloaded_urls.add(url)
                url_file_map[url] = (new_path, [(identifier, safe_name)])
                return {"identifier": identifier, "name": device_name, "version": version, "build": build,
                        "status": "exists", "size": filesize, "filepath": new_path, "sha1": sha1}
            log(f"  ~> Renaming old: {f.name} -> {filename}")
            f.rename(output_path)
            downloaded_urls.add(url)
            url_file_map[url] = (output_path, [(identifier, safe_name)])
            return {"identifier": identifier, "name": device_name, "version": version, "build": build,
                    "status": "exists", "size": filesize, "filepath": output_path, "sha1": sha1}

    old_files = [f for f in BASE_DIR.glob(f"*{identifier}*_Restore.ipsw") if identifier in f.stem]
    for old in old_files:
        if str(old) != str(output_path):
            log(f"  ~> Removing old: {old.name}")
            old.unlink()

    log(f"  [>] {identifier} ({device_name}) - v{version} ({build}) [{filesize/1024**3:.1f} GB]")

    dl_type, dl_tool = find_external_downloader()
    if dl_type == "idm":
        success, msg = download_with_idm(url, output_path, filesize)
    else:
        ensure_aria2()
        success, msg = download_with_aria2(url, output_path)

    if not success:
        return {"identifier": identifier, "name": device_name, "version": version, "status": "error",
                "error": msg, "size": filesize}

    downloaded_urls.add(url)
    url_file_map[url] = (output_path, [(identifier, safe_name)])
    return {"identifier": identifier, "name": device_name, "version": version, "build": build,
            "status": "downloaded", "size": filesize, "filepath": output_path, "sha1": sha1}


def verify_single(result):
    if result.get("status") not in ("downloaded", "exists"):
        return result
    fp = result.get("filepath")
    sha1 = result.get("sha1")
    sz = result.get("size")
    if fp and fp.exists():
        valid, msg = verify_file(fp, sha1=sha1, expected_size=sz)
        with results_lock:
            result["status"] = "verified" if valid else "corrupt"
            result["verification"] = msg
    else:
        with results_lock:
            result["status"] = "error"
            result["verification"] = "File not found"
    return result


def send_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(url, data=data, headers={"User-Agent": "firmware-downloader/1.0"})
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        log(f"[!] Telegram: {e}")


def main():
    signal.signal(signal.SIGINT, signal_handler)

    print("=" * 70)
    print("  Apple iPhone Firmware Downloader")
    print(f"  Download folder: {BASE_DIR}")
    print(f"  Log file:        {LOG_FILE}")
    print("=" * 70)

    if LOG_FILE.exists():
        LOG_FILE.unlink()

    dl_type, dl_tool = find_external_downloader()
    if dl_type == "idm":
        log(f"[+] External downloader: IDM ({dl_tool})")
    else:
        log("[*] No IDM found, will use aria2")
        ensure_aria2()

    log("[*] Fetching device list...")
    try:
        all_devices = get_devices()
    except Exception as e:
        log(f"[!] Failed: {e}")
        sys.exit(1)

    iphones = [d for d in all_devices if d["name"].startswith("iPhone")]
    iphones = [d for d in iphones if re.match(r"iPhone(\d+)", d["identifier"]) and int(re.match(r"iPhone(\d+)", d["identifier"]).group(1)) >= 7]
    iphones.sort(key=lambda d: d["identifier"], reverse=True)
    total = len(iphones)
    log(f"[+] Found {total} iPhone models")

    active_ids = set(d["identifier"] for d in iphones)
    cleaned = 0
    for f in BASE_DIR.glob("*.ipsw"):
        if not any(f.stem.startswith(aid) for aid in active_ids):
            log(f"  ~> Removing orphaned: {f.name}")
            f.unlink()
            cleaned += 1
    if cleaned:
        log(f"[+] Cleaned {cleaned} orphaned firmware(s)\n")
    else:
        log("")

    results.clear()
    start_time = time.time()
    verify_threads = []

    for i, dev in enumerate(iphones, 1):
        if stop_requested:
            break
        result = download_device(dev)
        if result is None:
            continue
        results.append(result)

        icon = {"exists": "[=]", "downloaded": "[>]", "error": "[x]", "skipped": "[-]"}
        i_icon = icon.get(result["status"], "[?]")
        ver = result.get("version", "?")
        msg = result.get("error", "")
        extra = f" - {msg}" if msg else ""
        log(f"  {i_icon} [{i}/{total}] {result['identifier']} v{ver}{extra}")

        if result["status"] == "downloaded":
            t = threading.Thread(target=verify_single, args=(result,), daemon=True)
            t.start()
            verify_threads.append(t)

    log("\n[*] Waiting for SHA1 verification to finish...")
    for t in verify_threads:
        t.join(timeout=600)

    elapsed = time.time() - start_time

    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)

    verified = sum(1 for r in results if r["status"] == "verified")
    exists = sum(1 for r in results if r["status"] == "exists")
    downloaded = sum(1 for r in results if r["status"] == "downloaded")
    errors = sum(1 for r in results if r["status"] == "error")
    corrupt = sum(1 for r in results if r["status"] == "corrupt")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    total_size = sum(r.get("size", 0) for r in results if r.get("size"))

    print(f"  Downloaded & verified:  {verified}")
    print(f"  Already up to date:     {exists}")
    print(f"  Errors:                 {errors}")
    print(f"  Corrupted:              {corrupt}")
    print(f"  Skipped:                {skipped}")
    if downloaded:
        print(f"  Pending verification:   {downloaded}")
    print(f"  Total firmware size:    {total_size/1024**3:.1f} GB")
    print(f"  Total time:             {elapsed/60:.1f} minutes")
    print("=" * 70)

    issues = [r for r in results if r["status"] in ("error", "corrupt")]
    if issues:
        print("\n  Devices with issues:")
        for r in issues:
            m = r.get("verification", r.get("error", ""))
            print(f"    - {r['identifier']} ({r['name']}): {m}")
        print()

    has_issues = any(r["status"] in ("error", "corrupt", "skipped") for r in results)
    msg = "\n".join([
        "Apple iPhone Firmware Download",
        f"{'✅ All devices OK' if not has_issues else '⚠️ Some devices have issues'}",
        f"Total: {len(results)}/{total} devices",
        f"{'✓' if verified else ''} Verified: {verified}  |  Up to date: {exists}  |  Errors: {errors}  |  Corrupted: {corrupt}  |  Skipped: {skipped}",
        f"Size: {total_size/1024**3:.1f} GB  |  Time: {elapsed/60:.1f} min",
    ])
    send_telegram(msg)

    if verified == total:
        log(f"[+] All {total} files up to date and verified ✓")
    elif verified > 0:
        log(f"[+] {verified} new download(s) verified successfully ✓")
    log(f"[+] Log saved to {LOG_FILE}")


if __name__ == "__main__":
    main()
