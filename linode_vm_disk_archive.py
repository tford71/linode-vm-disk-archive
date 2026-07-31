#!/usr/bin/env python3
"""Experimental Version 2.0 beta: archive and restore Linode local boot disks.

The opt-in ``archive --resize min`` path supports carefully constrained
whole-device ext4 compact archives. Version 1.0 remains the production path.
"""

import argparse
import json
import math
import os
import pathlib
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = "https://api.linode.com/v4"
SCRIPT_REVISION = "2.0"
ACTIVE_CLEANUPS = []
# Fresh local disks in this account/region provision 32 MiB below the requested
# API size. Request the cushion so an exact raw copy still fits.
LOCAL_DISK_CUSHION_MB = 32
# Fresh local disks can expose 16 MiB fewer usable raw bytes than their API
# allocation. Keep this separate from the larger allocation cushion used when
# a new disk must be made bigger than the archived root allocation.
FRESH_DISK_UNDERSIZE_MB = 16
# v2 shrink archives will round to a whole BSV GiB after adding this buffer.
# The read-only beta inspection reports the proposed resulting allocation.
RESIZE_SAFETY_BUFFER_MB = 4096
# Akamai Cloud Block Storage volumes cannot be created below 10 GB.  This is
# separate from the ext4 minimum: a compact filesystem can be smaller but its
# archive BSV must still meet the provider's minimum.
MINIMUM_BSV_SIZE_GB = 10

# The API's local-disk allocation can be slightly larger than the raw block
# device it presents to a guest. Keep an explicit allowance when sizing an
# ext4 filesystem inside a newly reduced local disk.
COMPACT_RAW_DEVICE_ALLOWANCE_MB = FRESH_DISK_UNDERSIZE_MB
VOLUME_LABEL_MAX = 32
LINODE_LABEL_MAX = 64

def milestone(message):
    print(f"[{datetime.now().astimezone().strftime('%H:%M:%S')}] {message}", flush=True)

class Linode:
    def __init__(self, token: str): self.token = token
    def request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(API + path, data=data, method=method,
          headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json",
                   "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r) if r.readable() else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise RuntimeError(f"{method} {path}: HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"{method} {path}: network failure: {e.reason}") from e
        except (socket.timeout, TimeoutError) as e:
            raise RuntimeError(f"{method} {path}: network request timed out") from e
    def get(self, path): return self.request("GET", path)
    def post(self, path, body): return self.request("POST", path, body)
    def put(self, path, body): return self.request("PUT", path, body)
    def delete(self, path): return self.request("DELETE", path)

def load_config(path):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Configuration file not found: {path}. Create config.json here or pass --config PATH.") from exc
def out_dir(c):
    p = pathlib.Path(c.get("archive_directory", "./archives")); p.mkdir(parents=True, exist_ok=True); return p
def validate_volume_label(label, description="Archive volume name"):
    """Reject an explicit BSV label before any cloud resource is created."""
    if not isinstance(label, str) or not 1 <= len(label) <= VOLUME_LABEL_MAX:
        raise RuntimeError(f"{description} must be 1-{VOLUME_LABEL_MAX} characters (received {len(label) if isinstance(label, str) else 'non-text'}).")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", label) or "--" in label or "__" in label:
        raise RuntimeError(f"{description} must start with a letter and contain only letters, numbers, single hyphens, or single underscores.")

def validate_linode_label(label, description="New VM name"):
    """Reject an explicit VM label before creating the restored Linode."""
    if not isinstance(label, str) or not 3 <= len(label) <= LINODE_LABEL_MAX:
        raise RuntimeError(f"{description} must be 3-{LINODE_LABEL_MAX} characters (received {len(label) if isinstance(label, str) else 'non-text'}).")
    if (not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]*[A-Za-z0-9]", label)
            or "--" in label or "__" in label or ".." in label):
        raise RuntimeError(f"{description} must start and end with an alphanumeric character and contain only letters, numbers, periods, single hyphens, or single underscores.")

def normalized_label_stem(value, volume=False):
    """Make an API-safe stem from source metadata without losing suffix space."""
    permitted = r"A-Za-z0-9_-" if volume else r"A-Za-z0-9._-"
    stem = re.sub(fr"[^{permitted}]+", "-", str(value or ""))
    stem = re.sub(r"[-_.]{2,}", "-", stem).strip("-_.")
    if not stem or not stem[0].isalpha():
        stem = "archive" if volume else "restored"
    return stem

def derived_volume_label(source_label, suffix):
    stem = normalized_label_stem(source_label, volume=True)
    available = VOLUME_LABEL_MAX - len(suffix)
    stem = stem[:available].rstrip("-_")
    candidate = f"{stem or 'a'}{suffix}"
    validate_volume_label(candidate, "Generated archive volume name")
    return candidate

def derived_linode_label(source_label, suffix):
    stem = normalized_label_stem(source_label)
    available = LINODE_LABEL_MAX - len(suffix)
    stem = stem[:available].rstrip("-_.")
    candidate = f"{stem or 'r'}{suffix}"
    validate_linode_label(candidate, "Generated restored VM name")
    return candidate

def next_archive_label(api, source_label):
    """Use a valid, suffix-preserving <source>-archive[-vN] BSV label."""
    labels = {v.get("label") for v in api.get("/volumes?page=1&page_size=100")["data"]}
    number = 1
    while True:
        suffix = "-archive" if number == 1 else f"-archive-v{number}"
        candidate = derived_volume_label(source_label, suffix)
        if candidate not in labels:
            return candidate
        number += 1

def next_restore_label(api, source_label):
    """Return the first unused <source-label>-rN label across the account."""
    if not source_label:
        raise RuntimeError("Archive metadata has no source label; pass --new-vm-name explicitly.")
    labels, page = set(), 1
    while True:
        response = api.get(f"/linode/instances?page={page}&page_size=100")
        labels.update(node.get("label") for node in response.get("data", []) if node.get("label"))
        if page >= response.get("pages", 1):
            break
        page += 1
    number = 1
    while True:
        candidate = derived_linode_label(source_label, f"-r{number}")
        if candidate not in labels:
            return candidate
        number += 1


def linode_by_label(api, label):
    """Resolve one exact source label, never guessing between duplicates."""
    matches, page = [], 1
    while True:
        response = api.get(f"/linode/instances?page={page}&page_size=100")
        matches.extend(node for node in response.get("data", []) if node.get("label") == label)
        if page >= response.get("pages", 1):
            break
        page += 1
    if not matches:
        raise RuntimeError(f"No Linode has the exact label {label!r}. Use --linode-id to select it directly.")
    if len(matches) > 1:
        ids = ", ".join(str(node["id"]) for node in matches)
        raise RuntimeError(f"More than one Linode has the label {label!r} (IDs: {ids}). Use --linode-id.")
    return matches[0]
def wait_until(fn, description, seconds=900, heartbeat=True):
    milestone(f"Waiting for {description}...")
    end = time.time() + seconds
    next_update = time.time() + 60
    while time.time() < end:
        value = fn()
        if value: return value
        if heartbeat and time.time() >= next_update:
            milestone(f"Still waiting for {description}...")
            next_update = time.time() + 60
        time.sleep(5)
    raise TimeoutError(f"Timed out waiting for {description}")
def wait_for_linode_create(api, node_id, started_at, resource_name="Linode"):
    """Do not add disks until the provider has completed node creation."""
    seen = {"event_id": None, "percent": None}
    started_wall = time.time()
    status_fallback_reported = False
    def terminal_event():
        nonlocal status_fallback_reported
        try:
            events = api.get("/account/events?page_size=100")["data"]
        except RuntimeError as exc:
            raise RuntimeError("Cannot verify Linode creation through Account Events. Add events:read_only to the API token, then retry. " + str(exc)) from exc
        for event in events:
            entity = event.get("entity") or {}
            created = datetime.fromisoformat(event["created"].replace("Z", "+00:00"))
            created = created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created.astimezone(timezone.utc)
            if entity.get("type") != "linode" or entity.get("id") != node_id or event.get("action") != "linode_create" or created < started_at:
                continue
            if seen["event_id"] != event["id"]:
                seen["event_id"] = event["id"]
                milestone(f"Create event {event['id']} started")
            percent = event.get("percent_complete")
            if percent is not None and percent != seen["percent"]:
                seen["percent"] = percent; milestone(f"Create progress: {percent}%")
            if event.get("status") == "failed":
                raise RuntimeError(f"{resource_name} creation failed. Provider event: " + json.dumps(event, sort_keys=True))
            if event.get("status") == "finished":
                return api.get(f"/linode/instances/{node_id}")
        # Events can arrive late or be omitted from a token's event view even
        # though the API already reports the new Linode as ready.  The later
        # disk/config calls still have their own provider-busy retries.
        if time.time() - started_wall >= 30:
            node = api.get(f"/linode/instances/{node_id}")
            if node.get("status") in ("offline", "running"):
                if not status_fallback_reported:
                    milestone(f"Create Event is not visible yet; provider reports Linode {node_id} is {node['status']}. Continuing safely.")
                    status_fallback_reported = True
                return node
        return None
    return wait_until(terminal_event, f"creation of {resource_name} {node_id}", seconds=1800)
def disk_for_id(api, linode_id, disk_id):
    for d in api.get(f"/linode/instances/{linode_id}/disks")["data"]:
        if d["id"] == disk_id: return d
    raise RuntimeError(f"Disk {disk_id} is not on Linode {linode_id}")
def source_boot_config(api, c, linode_id):
    """Resolve the selected/last-booted configuration without guessing."""
    configs = api.get(f"/linode/instances/{linode_id}/configs")["data"]
    if c.get("source_config_id"):
        configs = [x for x in configs if x["id"] == int(c["source_config_id"])]
    else:
        last = [x for x in configs if x.get("last_booted")]
        if len(last) == 1: configs = last
    if len(configs) != 1:
        raise RuntimeError("Cannot safely choose a boot configuration. Set source_config_id or source_root_disk_id.")
    return configs[0]

def source_root_disk(api, c, linode_id, config=None):
    """Resolve the root local disk from the selected source configuration."""
    if c.get("source_root_disk_id"):
        return disk_for_id(api, linode_id, int(c["source_root_disk_id"]))
    config = config or source_boot_config(api, c, linode_id)
    slot = config.get("root_device", "/dev/sda").rsplit("/", 1)[-1]
    device = config.get("devices", {}).get(slot)
    if not device or not device.get("disk_id"):
        raise RuntimeError(f"Config {config['id']} root device {slot} is not a local disk. Set source_root_disk_id explicitly.")
    return disk_for_id(api, linode_id, device["disk_id"])

def source_swap_disk(api, linode_id, config, root):
    """Return one separately configured local swap disk, if present."""
    swaps = []
    for slot, device in (config.get("devices") or {}).items():
        if not device or not device.get("disk_id") or device["disk_id"] == root["id"]:
            continue
        disk = disk_for_id(api, linode_id, device["disk_id"])
        if disk.get("filesystem") == "swap":
            swaps.append({"disk": disk, "slot": slot})
    if len(swaps) > 1:
        raise RuntimeError("More than one separate swap disk is configured; Version 1.0 supports one separate swap disk only.")
    return swaps[0] if swaps else None

def source_inventory(api, linode_id, config, root, swap_source):
    """Describe exactly what this root-disk-only archive does and does not preserve."""
    root_slot = config.get("root_device", "/dev/sda")
    root_slot = root_slot.rsplit("/", 1)[-1]
    attached_volumes, extra_local = [], []
    swap_id = swap_source["disk"]["id"] if swap_source else None
    for slot, device in (config.get("devices") or {}).items():
        if not device or slot == root_slot:
            continue
        if device.get("volume_id"):
            volume_id = device["volume_id"]
            try:
                volume = api.get(f"/volumes/{volume_id}")
                attached_volumes.append((slot, volume.get("label", str(volume_id)), volume.get("size")))
            except Exception:
                attached_volumes.append((slot, f"volume {volume_id}", None))
        elif device.get("disk_id") and device["disk_id"] != swap_id:
            disk = disk_for_id(api, linode_id, device["disk_id"])
            extra_local.append((slot, disk.get("label", str(disk["id"])), disk.get("size"), disk.get("filesystem")))
    return root_slot, attached_volumes, extra_local

def inspect_resize_candidate(c, ip, helper_volume_label):
    """Read the detached source root device without modifying its filesystem."""
    inspector = r'''set -Eeuo pipefail
fail() { printf '{"ok":false,"error":"%s"}\n' "$1"; exit 0; }
disk_name() {
  local device="$1" parent
  parent=$(lsblk -dno PKNAME "$device" 2>/dev/null || true)
  [ -n "$parent" ] && { echo "$parent"; return; }
  basename "$device" | sed 's/[0-9]*$//'
}
helper_link="/dev/disk/by-id/scsi-0Linode_Volume_$1"
[ -e "$helper_link" ] || fail "helper BSV by-id link is absent"
root_disk=$(disk_name "$(findmnt -n -o SOURCE /)")
volume_disks=()
for link in /dev/disk/by-id/scsi-0Linode_Volume_*; do
  [ -e "$link" ] || continue
  case "$link" in *-part*) continue;; esac
  volume_disks+=("$(disk_name "$(readlink -f "$link")")")
done
is_volume_disk() { local candidate="$1" disk; for disk in "${volume_disks[@]}"; do [ "$candidate" = "$disk" ] && return 0; done; return 1; }
candidates=()
while read -r name type; do
  [ "$type" = disk ] || continue
  [ "$name" = "$root_disk" ] && continue
  is_volume_disk "$name" && continue
  bytes=$(blockdev --getsize64 "/dev/$name")
  candidates+=("$bytes:$name")
done < <(lsblk -dn -o NAME,TYPE)
[ "${#candidates[@]}" -gt 0 ] || fail "no non-helper local disk is available for inspection"
IFS=$'\n' candidates=($(sort -nr <<<"${candidates[*]}")); unset IFS
if [ "${#candidates[@]}" -gt 1 ]; then
  first=${candidates[0]%%:*}; second=${candidates[1]%%:*}
  [ "$first" -gt "$second" ] || fail "source local disk is ambiguous after excluding the helper BSV"
fi
source_disk=${candidates[0]#*:}
source="/dev/$source_disk"
raw_bytes=$(blockdev --getsize64 "$source")
fstype=$(blkid -s TYPE -o value "$source" 2>/dev/null || true)
children=$(lsblk -nr -o NAME "$source" | sed '1d' | wc -l)
mounted=$(findmnt -rn -S "$source" 2>/dev/null || true)
if [ "$fstype" != ext4 ]; then
  printf '{"ok":true,"supported":false,"source_device":"%s","raw_bytes":%s,"filesystem":"%s","reason":"root device is not whole-device ext4"}\n' "$source" "$raw_bytes" "${fstype:-unknown}"
  exit 0
fi
if [ "$children" -ne 0 ]; then
  printf '{"ok":true,"supported":false,"source_device":"%s","raw_bytes":%s,"filesystem":"ext4","reason":"root device has partitions or child block devices"}\n' "$source" "$raw_bytes"
  exit 0
fi
if [ -n "$mounted" ]; then
  printf '{"ok":true,"supported":false,"source_device":"%s","raw_bytes":%s,"filesystem":"ext4","reason":"source root device is unexpectedly mounted in helper"}\n' "$source" "$raw_bytes"
  exit 0
fi
command -v resize2fs >/dev/null 2>&1 || fail "helper BSV lacks resize2fs; build a Version 2 helper before using --resize"
command -v tune2fs >/dev/null 2>&1 || fail "helper BSV lacks tune2fs; build a Version 2 helper before using --resize"
estimate=$(resize2fs -P "$source" 2>&1) || { printf '{"ok":true,"supported":false,"source_device":"%s","raw_bytes":%s,"filesystem":"ext4","reason":"resize2fs could not estimate the ext4 minimum"}\n' "$source" "$raw_bytes"; exit 0; }
minimum_blocks=$(printf '%s\n' "$estimate" | sed -n 's/.*: \([0-9][0-9]*\)$/\1/p' | tail -n 1)
block_size=$(tune2fs -l "$source" 2>/dev/null | awk -F: '/^Block size:/{gsub(/ /,"",$2); print $2}')
block_count=$(tune2fs -l "$source" 2>/dev/null | awk -F: '/^Block count:/{gsub(/ /,"",$2); print $2}')
uuid=$(blkid -s UUID -o value "$source" 2>/dev/null || true)
[ -n "$uuid" ] || fail "could not parse ext4 filesystem UUID"
case "$minimum_blocks:$block_size:$block_count" in
  *[!0-9:]*|::*|:*|*:) fail "could not parse numeric ext4 block metrics" ;;
esac
minimum_bytes=$((minimum_blocks * block_size))
filesystem_bytes=$((block_count * block_size))
printf '{"ok":true,"supported":true,"source_device":"%s","raw_bytes":%s,"filesystem":"ext4","layout":"whole-device","ext4_uuid":"%s","ext4_block_size":%s,"ext4_block_count":%s,"ext4_filesystem_bytes":%s,"ext4_minimum_blocks":%s,"ext4_minimum_bytes":%s}\n' "$source" "$raw_bytes" "$uuid" "$block_size" "$block_count" "$filesystem_bytes" "$minimum_blocks" "$minimum_bytes"'''
    result = ssh(c, ip, f"/bin/bash -c {shlex.quote(inspector)} -- {shlex.quote(helper_volume_label)}")
    if result.returncode != 0:
        raise RuntimeError("Resize inspection SSH command failed: " + (result.stderr.strip() or result.stdout.strip()))
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Resize inspection returned invalid JSON: " + result.stdout.strip()) from exc
    if not data.get("ok"):
        raise RuntimeError("Resize inspection failed: " + data.get("error", "unknown helper error"))
    return data

def validate_resize_root(inspection, expected_api_mb, stage, expected_uuid=None, maximum_filesystem_bytes=None):
    """Prove that helper-side discovery found the intended local ext4 root.

    Local disks lack a provider by-id link, so this is deliberately stricter
    than choosing the largest remaining disk.  It ties the discovered device
    to the configured root allocation and preserves its ext4 identity across
    every destructive phase.
    """
    if not inspection.get("supported"):
        raise RuntimeError(f"{stage}: unsupported helper-side root discovery: " + inspection.get("reason", "unknown reason"))
    expected_max = int(expected_api_mb) * 1024 * 1024
    expected_min = expected_max - LOCAL_DISK_CUSHION_MB * 1024 * 1024
    raw_bytes = int(inspection["raw_bytes"])
    if not expected_min <= raw_bytes <= expected_max:
        raise RuntimeError(
            f"{stage}: discovered local ext4 root {inspection['source_device']} is {raw_bytes} bytes, "
            f"outside the expected {expected_min}-{expected_max} byte range for the configured root disk."
        )
    if expected_uuid and inspection.get("ext4_uuid") != expected_uuid:
        raise RuntimeError(
            f"{stage}: discovered ext4 UUID {inspection.get('ext4_uuid')} does not match the original source UUID {expected_uuid}."
        )
    filesystem_bytes = int(inspection["ext4_filesystem_bytes"])
    if filesystem_bytes > raw_bytes:
        raise RuntimeError(f"{stage}: ext4 filesystem ({filesystem_bytes} bytes) exceeds its local block device ({raw_bytes} bytes).")
    if maximum_filesystem_bytes is not None and filesystem_bytes > int(maximum_filesystem_bytes):
        raise RuntimeError(
            f"{stage}: ext4 filesystem ({filesystem_bytes} bytes) exceeds the planned compact size "
            f"({int(maximum_filesystem_bytes)} bytes)."
        )
    return inspection

def plan_resize(api, c):
    """Read-only qualification for an opt-in ext4 shrink archive."""
    node = api.get(f"/linode/instances/{c['source_linode_id']}")
    boot_config = source_boot_config(api, c, node["id"])
    root = source_root_disk(api, c, node["id"], boot_config)
    swap_source = source_swap_disk(api, node["id"], boot_config, root)
    summary = (
        "This beta inspection will power off the source VM, boot the reusable helper, "
        "and inspect the detached root filesystem. It will not run e2fsck, resize2fs, "
        "or create an archive BSV. The source VM will remain powered off when complete.\n\n"
        f"Source: {node['label']} (Linode {node['id']})\n"
        f"API root allocation: {root['size']} MB\n"
    )
    if swap_source:
        summary += f"Separate swap: {swap_source['disk']['size']} MB\n"
    print(summary, flush=True)
    require_yes(f"INSPECT RESIZE {node['id']}")
    helper = helper_boot_volume(api, c, node["region"])
    shutdown(api, node["id"])
    config = create_config(
        api, node["id"], "cold-archive-v2-resize-inspect",
        {"volume_id": helper["id"]}, {"disk_id": root["id"]},
        {"disk_id": swap_source["disk"]["id"]} if swap_source else None,
        kernel="linode/grub2",
    )
    ACTIVE_CLEANUPS.append(lambda: cleanup_bsv_session(api, node["id"], config["id"], [helper["id"]]))
    milestone("Booting source from reusable Block Storage helper for read-only resize inspection")
    boot(api, node["id"], config["id"], "read-only resize inspection")
    node = api.get(f"/linode/instances/{node['id']}")
    wait_ssh(c, node["ipv4"][0], "resize-inspection helper")
    milestone("Helper is SSH-ready; measuring the detached root filesystem")
    inspection = inspect_resize_candidate(c, node["ipv4"][0], helper["label"])
    shutdown(api, node["id"])
    api.post(f"/volumes/{helper['id']}/detach", {})
    api.delete(f"/linode/instances/{node['id']}/configs/{config['id']}")
    ACTIVE_CLEANUPS.clear()
    inspection["source_api_allocation_mb"] = root["size"]
    inspection["safety_buffer_mb"] = RESIZE_SAFETY_BUFFER_MB
    if inspection.get("supported"):
        minimum_mb = math.ceil(int(inspection["ext4_minimum_bytes"]) / (1024 * 1024))
        calculated_compact_mb = math.ceil((minimum_mb + RESIZE_SAFETY_BUFFER_MB) / 1024) * 1024
        compact_bsv_gb = max(MINIMUM_BSV_SIZE_GB, calculated_compact_mb // 1024)
        inspection["ext4_minimum_mb"] = minimum_mb
        inspection["calculated_compact_root_mb"] = calculated_compact_mb
        inspection["provider_minimum_bsv_gb"] = MINIMUM_BSV_SIZE_GB
        inspection["proposed_compact_root_mb"] = compact_bsv_gb * 1024
        inspection["proposed_archive_bsv_gb"] = compact_bsv_gb
        milestone("Resize qualification passed; no filesystem changes were made")
    else:
        milestone("Resize qualification did not support this root layout; no filesystem changes were made")
    print(json.dumps(inspection, indent=2))
def all_plan_types(api):
    """Return the type catalogue.  There are currently fewer than 500 types."""
    return api.get("/linode/types?page_size=500")["data"]
def plan(api, plan_id):
    return next((item for item in all_plan_types(api) if item["id"] == plan_id), None) or (_ for _ in ()).throw(RuntimeError(f"Unknown Linode plan: {plan_id}"))
def plan_availability_in(value, known_ids):
    """Read explicit ``plan`` / ``available`` records without losing false.

    The regional availability endpoint contains entries such as
    ``{"plan": "g7-premium-4", "available": false}``.  The earlier generic
    recursive parser found the string plan ID but discarded the boolean, which
    accidentally advertised unavailable premium and GPU plans.
    """
    found = {}
    if isinstance(value, dict):
        plan_id = value.get("plan") or value.get("plan_id") or value.get("type")
        available = value.get("available")
        if isinstance(plan_id, str) and plan_id in known_ids and isinstance(available, bool):
            found[plan_id] = available
        for child in value.values():
            for plan_id, is_available in plan_availability_in(child, known_ids).items():
                # An explicit unavailable result is conservative and wins over
                # a contradictory response from a less-specific endpoint.
                found[plan_id] = found.get(plan_id, True) and is_available
    elif isinstance(value, list):
        for child in value:
            for plan_id, is_available in plan_availability_in(child, known_ids).items():
                found[plan_id] = found.get(plan_id, True) and is_available
    return found

def region_plan_availability(api, region, types):
    """Return explicit per-plan regional availability statuses when supplied.

    The provider documents this endpoint primarily for high-demand premium and
    GPU types. Plans absent from the response remain catalogue candidates;
    plans explicitly marked unavailable are never shown.
    """
    known = {p["id"] for p in types}
    statuses, notes = {}, []
    for endpoint in (f"/regions/{region}/availability", f"/account/availability/{region}"):
        try:
            response_statuses = plan_availability_in(api.get(endpoint), known)
            for plan_id, is_available in response_statuses.items():
                statuses[plan_id] = statuses.get(plan_id, True) and is_available
            notes.append(endpoint)
        except RuntimeError:
            pass
    return statuses, notes
def price_text(p, region=None):
    # Regional price overrides apply only to the requested restore region.
    price = next((x for x in p.get("region_prices", []) if x.get("id") == region), None) if region else None
    price = price or p.get("price")
    if isinstance(price, dict):
        monthly = price.get("monthly")
        if monthly is not None: return f"${monthly}/mo"
        hourly = price.get("hourly")
        if hourly is not None: return f"${hourly}/hr"
    if isinstance(price, list):
        usd = next((x for x in price if x.get("currency") == "USD"), None)
        if usd and usd.get("monthly") is not None: return f"${usd['monthly']}/mo"
    return "price unavailable"
def restore_candidates(api, region, required_mb):
    types = all_plan_types(api)
    availability, availability_sources = region_plan_availability(api, region, types)
    candidates = [p for p in types if p.get("disk", 0) >= required_mb
                  and availability.get(p["id"], True)]
    def natural_parts(value):
        """Sort plan IDs such as g6-standard-8 before g6-standard-16."""
        import re
        return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value.lower()))

    def family_key(item):
        name = item["id"].lower()
        if "vpu" in name or item.get("accelerated_devices", 0): group = 3
        elif item.get("gpus", 0): group = 4
        elif name.startswith("g6-"): group = 0
        elif name.startswith("g7-"): group = 1
        elif name.startswith("g8-"): group = 2
        else: group = 5
        return group, natural_parts(name)
    candidates.sort(key=family_key)
    return candidates, availability_sources, bool(availability)
def helper_builder_candidates(api, region, required_mb):
    candidates, _sources, _filtered = restore_candidates(api, region, required_mb)
    family_order = ("g6-", "g7-", "g8-")
    candidates = [p for p in candidates if p["id"].startswith(family_order)]
    # Prefer a standard plan over a dedicated one when local disk and memory
    # are equal; the helper needs storage, not dedicated CPU.
    candidates.sort(key=lambda p: (next(i for i, f in enumerate(family_order) if p["id"].startswith(f)), p["disk"], p["memory"], 0 if "-standard-" in p["id"] else 1, p["id"]))
    return candidates
def minimum_restore_allocation_mb(m):
    source_mb = math.ceil(int(m["source_bytes"]) / (1024 * 1024))
    original = int(m.get("source_api_disk_mb") or 0)
    # Reusing the archived allocation is safe only when the source raw device
    # already sits at least one known fresh-disk undersize below it. Otherwise
    # request a larger local disk so its raw byte capacity can hold the copy.
    if original and int(m["source_bytes"]) <= (original - FRESH_DISK_UNDERSIZE_MB) * 1024 * 1024:
        return original
    return source_mb + LOCAL_DISK_CUSHION_MB

def choose_restore_disk_size(c, m):
    """Choose the target allocation before filtering eligible final plans.

    A compact archive holds a deliberately shrunken ext4 filesystem.  It can
    safely be restored either at its original allocation (the conservative
    default), its minimum restorable allocation, or a custom allocation selected after
    the final plan has been chosen.  Custom begins at the archived allocation
    so the plan list never includes a type too small for the payload.
    """
    original_mb = minimum_restore_allocation_mb(m)
    resize = m.get("resize")
    if not resize:
        c["restore_disk_mb"] = original_mb
        c["restore_size_mode"] = "original"
        return original_mb
    compact_mb = int(resize["compact_api_mb"])
    requested = c.get("restore_size")
    if requested not in (None, "original", "min", "custom"):
        raise RuntimeError("--restore-size must be original, min, or custom.")
    if requested is None:
        answer = input(
            f"This is a compact archive. Restore the original {original_mb} MB root allocation? [Y/n]: "
        ).strip().lower()
        requested = "original" if answer in ("", "y", "yes") else "min"
    c["restore_size_mode"] = requested
    # The final custom value is chosen after the plan is known.  Use the
    # archived allocation now to filter only plans able to hold the payload.
    c["restore_disk_mb"] = original_mb if requested == "original" else compact_mb
    milestone(
        f"Selected {'original' if requested == 'original' else 'minimum' if requested == 'min' else 'minimum before custom selection'} restored root allocation: "
        f"{c['restore_disk_mb']} MB"
    )
    return c["restore_disk_mb"]


def parse_restore_size_mb(text):
    """Accept one whole-number MB value from the explicit wizard range."""
    if not re.fullmatch(r"\s*[1-9][0-9]*\s*", text):
        raise ValueError("enter a whole number of MB")
    value = int(text.strip())
    if value <= 0:
        raise ValueError("size must be greater than zero")
    return value


def choose_custom_restore_size(c, m, final_plan):
    """Choose a compact-archive target after the operator knows the plan."""
    if c.get("restore_size_mode") != "custom":
        return int(c["restore_disk_mb"])
    minimum_mb = int(m["resize"]["compact_api_mb"])
    swap_mb = int((m.get("swap") or {}).get("size_mb", 0))
    maximum_mb = int(final_plan["disk"]) - swap_mb
    if maximum_mb < minimum_mb:
        raise RuntimeError(f"Plan {final_plan['id']} cannot hold the archived {minimum_mb} MB root allocation plus its {swap_mb} MB swap disk.")
    while True:
        answer = input(
            f"Enter a whole number of MB for {final_plan['id']} "
            f"between {minimum_mb} and {maximum_mb}: "
        ).strip()
        try:
            selected_mb = parse_restore_size_mb(answer)
        except ValueError as exc:
            print(f"Invalid size: {exc}.", flush=True)
            continue
        if selected_mb < minimum_mb:
            print(f"Size must be at least the archived {minimum_mb} MB allocation.", flush=True)
            continue
        if selected_mb > maximum_mb:
            print(f"Size exceeds {final_plan['id']}'s {maximum_mb} MB local-disk capacity.", flush=True)
            continue
        c["restore_disk_mb"] = selected_mb
        milestone(f"Selected custom restored root allocation: {selected_mb} MB")
        return selected_mb

def choose_restore_plan(api, c, m, excluded_plan_ids=None):
    """Interactively choose the final type after reading the volume's region."""
    excluded_plan_ids = set(excluded_plan_ids or ())
    volume = verify_archive_volume(api, m)
    target_mb = math.ceil(int(m["source_bytes"]) / (1024 * 1024))
    requested_target_mb = int(c["restore_disk_mb"])
    swap_mb = int((m.get("swap") or {}).get("size_mb", 0))
    required_plan_mb = requested_target_mb + swap_mb
    # This is the *final* plan.  Copying happens first on a separate automatic
    # g6/g7/g8 helper plan, so a final plan only needs room for the disk itself.
    candidates, sources, account_filtered = restore_candidates(api, volume["region"], required_plan_mb)
    candidates = [plan_type for plan_type in candidates if plan_type["id"] not in excluded_plan_ids]
    if not candidates:
        raise RuntimeError(f"No listed Linode type in {volume['region']} can hold the {requested_target_mb} MB restored root allocation plus {swap_mb} MB swap.")
    original_plan = m.get("source_plan")
    # A custom root size must choose the final plan first, so do not offer the
    # original-plan shortcut in that mode.  The source plan can still appear
    # as the numbered-list default when it meets the archived-payload floor.
    if original_plan and not c.get("restore_plan_explicit") and c.get("restore_size_mode") != "custom":
        original = next((p for p in candidates if p["id"] == original_plan), None)
        if original:
            answer = input(f"Restore with the original VM size/plan {original_plan} in {volume['region']} ({price_text(original, volume['region'])})? [Y/n]: ").strip().lower()
            if answer in ("", "y", "yes"):
                c["restore_plan"] = original_plan
                milestone(f"Selected archived source plan: {original_plan}")
                return volume
        else:
            milestone(f"Archived source plan {original_plan} is unavailable or too small in {volume['region']}; choose another plan.")
    milestone(f"Archive volume is in {volume['region']}; showing {len(candidates)} eligible restore types")
    if not account_filtered:
        milestone("The region availability API did not return per-plan availability; this is the region type catalogue and creation remains the final capacity check.")
    capacity_note = f" plus {swap_mb} MB swap" if swap_mb else ""
    print(f"\nEligible final restore plans (must hold the requested {requested_target_mb} MB local root allocation{capacity_note}):")
    for number, p in enumerate(candidates, 1):
        print(f"  {number:>2}. {p['id']:<32} {p['disk']:>8} MB local  {p['memory']:>8} MB RAM  {price_text(p, volume['region'])}")
    configured = c.get("restore_plan")
    default_number = next((i for i, p in enumerate(candidates, 1) if p["id"] == configured), None)
    suffix = f" [default {default_number}: {configured}]" if default_number else ""
    while True:
        choice = input(f"Select restore plan 1-{len(candidates)}{suffix}, or q to cancel: ").strip().lower()
        if not choice and default_number: choice = str(default_number)
        if choice == "q": raise RuntimeError("Cancelled.")
        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
            selected = candidates[int(choice) - 1]
            answer = input(f"Use {selected['id']} for the new VM in {volume['region']} ({price_text(selected, volume['region'])})? [y/N]: ").strip().lower()
            if answer in ("y", "yes"):
                c["restore_plan"] = selected["id"]
                return volume
            print("Plan not selected; choose again.")
        else:
            print("Enter a number from the displayed list, or q.")
def require_token():
    token = os.environ.get("LINODE_TOKEN")
    if not token: raise RuntimeError("Set LINODE_TOKEN in WSL; do not put it in the config file.")
    return token
def require_yes(words):
    """Require the exact confirmation text without treating a typo as an abort."""
    while True:
        answer = input(f"Type exactly {words!r} to continue (or CANCEL to abort): ").strip()
        if answer == words:
            return
        if answer.upper() == "CANCEL":
            raise RuntimeError("Cancelled by operator.")
        print("Confirmation did not match. Please try again, or type CANCEL to abort.", flush=True)
def post_when_linode_ready(api, path, body, description, seconds=900):
    """Linode operations can remain locked briefly after a finished Event."""
    end = time.time() + seconds
    next_notice = 0
    while True:
        try:
            return api.post(path, body)
        except RuntimeError as exc:
            if "Linode busy." not in str(exc) or time.time() >= end:
                raise
            if time.time() >= next_notice:
                milestone(f"Linode is still settling; waiting to {description}...")
                next_notice = time.time() + 30
            time.sleep(5)

def resize_disk_when_ready(api, node_id, disk_id, size_mb, description, seconds=1800):
    """Resize one powered-off local disk and wait for the API size to settle."""
    milestone(f"Resizing {description} to {size_mb} MB")
    end, next_notice = time.time() + seconds, 0
    while True:
        try:
            api.post(f"/linode/instances/{node_id}/disks/{disk_id}/resize", {"size": int(size_mb)})
            break
        except RuntimeError as exc:
            if "Linode busy." not in str(exc) or time.time() >= end:
                raise
            if time.time() >= next_notice:
                milestone(f"Linode is still settling; waiting to resize {description}...")
                next_notice = time.time() + 30
            time.sleep(5)
    while time.time() < end:
        disk = api.get(f"/linode/instances/{node_id}/disks/{disk_id}")
        if int(disk.get("size", 0)) == int(size_mb):
            milestone(f"{description.capitalize()} resize completed")
            return disk
        if time.time() >= next_notice:
            milestone(f"Waiting for {description} resize to complete...")
            next_notice = time.time() + 30
        time.sleep(5)
    raise TimeoutError(f"Timed out waiting for {description} resize to {size_mb} MB")

def delete_when_linode_ready(api, path, description, seconds=900):
    end = time.time() + seconds
    while True:
        try:
            return api.delete(path)
        except RuntimeError as exc:
            if "Linode busy." not in str(exc) or time.time() >= end:
                raise
            milestone(f"Linode is still settling; waiting to {description}...")
            time.sleep(5)

def detach_volume_and_wait(api, volume_id, description):
    """Detach a BSV before reusing it in a different temporary config."""
    api.post(f"/volumes/{volume_id}/detach", {})
    wait_until(lambda: api.get(f"/volumes/{volume_id}").get("linode_id") is None,
               f"{description} to detach")

def helper_ssh_command(c, ip, remote_command):
    # One builder IP deliberately boots several different temporary disks. Do
    # not retain its host key between those boots; the automation key still
    # authenticates us and no secrets are sent to the helper over this channel.
    return ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10", "-i", c["helper_ssh_private_key"], f"root@{ip}", remote_command]

def boot_event(api, node_id, config_id, excluded_ids):
    for event in api.get("/account/events?page=1&page_size=100")["data"]:
        entity, secondary = event.get("entity") or {}, event.get("secondary_entity") or {}
        if (event.get("id") not in excluded_ids and event.get("action") == "linode_boot"
                and entity.get("id") == node_id and secondary.get("id") == config_id):
            return event
    return None

def boot(api, node_id, config_id, purpose="configuration"):
    """Boot once, inspect its provider Event, then make one safe retry if needed."""
    for attempt in (1, 2):
        known = {event.get("id") for event in api.get("/account/events?page=1&page_size=100")["data"]}
        milestone(f"Booting Linode {node_id} using {purpose}" + (" (retry)" if attempt == 2 else ""))
        post_when_linode_ready(api, f"/linode/instances/{node_id}/boot", {"config_id": config_id}, "boot the Linode")
        end, next_notice = time.time() + 900, time.time() + 60
        while time.time() < end:
            node = api.get(f"/linode/instances/{node_id}")
            if node.get("status") == "running": return node
            event = boot_event(api, node_id, config_id, known)
            if event and event.get("status") == "failed":
                if attempt == 2:
                    raise RuntimeError("Linode boot failed after retry. Provider event: " + json.dumps(event, sort_keys=True))
                milestone("Provider reported the first boot failed; waiting briefly, then retrying once.")
                time.sleep(30)
                break
            if time.time() >= next_notice:
                milestone(f"Still waiting for Linode {node_id} to boot...")
                next_notice = time.time() + 60
            time.sleep(5)
        else:
            if attempt == 2: raise TimeoutError(f"Timed out waiting for Linode {node_id} to boot")
            milestone("First boot did not reach running state; retrying once after the provider settles.")
            time.sleep(30)
def shutdown(api, node_id):
    node = api.get(f"/linode/instances/{node_id}")
    if node["status"] != "offline":
        milestone(f"Powering off Linode {node_id}")
        api.post(f"/linode/instances/{node_id}/shutdown", {})
        wait_until(lambda: api.get(f"/linode/instances/{node_id}").get("status") == "offline", f"Linode {node_id} to stop")

def cleanup_bsv_session(api, node_id, config_id, volume_ids):
    """Best-effort cleanup for helper configs and attachments after an error."""
    try: shutdown(api, node_id)
    except Exception: pass
    for volume_id in volume_ids:
        try: api.post(f"/volumes/{volume_id}/detach", {})
        except Exception: pass
    if config_id:
        try: api.delete(f"/linode/instances/{node_id}/configs/{config_id}")
        except Exception: pass

def run_active_cleanups():
    while ACTIVE_CLEANUPS:
        try: ACTIVE_CLEANUPS.pop()()
        except Exception: pass

def archive_plan_bsv(api, c):
    """Read-only archive preflight."""
    node = api.get(f"/linode/instances/{c['source_linode_id']}")
    root = source_root_disk(api, c, node["id"])
    print(json.dumps({"source": {"id": node["id"], "label": node["label"], "region": node["region"], "plan": node["type"]}, "root_disk": {"id": root["id"], "size_mb": root["size"]}, "archive_volume_gb": math.ceil(root["size"] / 1024), "will_delete_source": bool(c.get("delete_source"))}, indent=2))
    return node, root

def prepare_bsv_helper(api, c):
    """Build and retain the regional helper BSV; no source/archive data moves."""
    source = api.get(f"/linode/instances/{c['source_linode_id']}")
    volume = helper_boot_volume(api, c, source["region"])
    print(json.dumps({"helper_boot_volume_id": volume["id"], "label": volume["label"], "region": volume["region"], "size_gb": volume["size"], "tags": volume.get("tags", [])}, indent=2))

def archive_tags(node, source_bytes, source_api_mb, boot_config, verified_sha256, swap=None, resize=None):
    source_mb = math.ceil(source_bytes / (1024 * 1024))
    source_label_tag = f"ca-source-label-{node['label']}"
    tags = [
        "cold-archive", "ca-format-2b1" if resize else "ca-format-1", source_label_tag,
        f"ca-source-plan-{node['type']}", f"ca-source-region-{node['region']}",
        f"ca-source-linode-{node['id']}", f"ca-source-disk-mb-{source_mb}",
        f"ca-source-bytes-{source_bytes}", f"ca-root-api-mb-{source_api_mb}",
        # Linode tags are limited to 50 characters. Store the 64-character
        # SHA-256 in two fixed, independently recognizable fragments.
        "ca-verify-v1", f"ca-vhash1-{verified_sha256[:32]}", f"ca-vhash2-{verified_sha256[32:]}",
        f"ca-boot-kernel-{boot_config['kernel'].replace('linode/', '')}",
        f"ca-boot-root-{boot_config.get('root_device', '/dev/sda').rsplit('/', 1)[-1]}",
    ]
    if swap:
        tags.extend([f"ca-swap1-mb-{swap['size_mb']}", f"ca-swap1-slot-{swap['slot']}", f"ca-swap1-uuid-{swap['uuid']}"])
    if resize:
        # A Linode label may be 64 characters while a tag is capped at 50.
        # Preserve every character in two bounded fragments for compact beta
        # archives rather than silently truncating the restored VM's default.
        if len(source_label_tag) > 50:
            tags.remove(source_label_tag)
            tags.extend([f"ca-source-label1-{node['label'][:32]}", f"ca-source-label2-{node['label'][32:]}" ])
        tags.extend([
            "ca-resize-v1", f"ca-resize-mode-{resize['mode']}",
            f"ca-resize-fs-{resize['filesystem']}", f"ca-resize-layout-{resize['layout']}",
            f"ca-resize-original-mb-{resize['original_api_mb']}",
            f"ca-resize-compact-mb-{resize['compact_api_mb']}",
            f"ca-resize-buffer-mb-{resize['safety_buffer_mb']}",
        ])
    return tags

def single_ca_tag_value(tags, prefix):
    """Return one ca-* value, never silently choosing among duplicates."""
    matches = [tag[len(prefix):] for tag in tags if tag.startswith(prefix)]
    if len(matches) > 1:
        raise RuntimeError(f"Archive volume has multiple {prefix}* tags. Remove the duplicate before restoring.")
    return matches[0] if matches else None

def validate_archive_metadata_tags(tags):
    """Reject ambiguous archive metadata before any restore decision is made."""
    for prefix in (
        "ca-format-", "ca-source-label-", "ca-source-plan-", "ca-source-region-",
        "ca-source-linode-", "ca-source-disk-mb-", "ca-source-bytes-",
        "ca-root-api-mb-", "ca-vhash1-", "ca-vhash2-", "ca-boot-kernel-",
        "ca-boot-root-", "ca-swap1-mb-", "ca-swap1-slot-", "ca-swap1-uuid-",
        "ca-source-label1-", "ca-source-label2-",
        "ca-resize-mode-", "ca-resize-fs-", "ca-resize-layout-", "ca-resize-original-mb-",
        "ca-resize-compact-mb-", "ca-resize-buffer-mb-",
    ):
        single_ca_tag_value(tags, prefix)
    if tags.count("ca-verify-v1") > 1:
        raise RuntimeError("Archive volume has multiple ca-verify-v1 tags. Remove the duplicate before restoring.")

def verified_hash_from_tags(tags):
    """Return a complete verified SHA-256 only when both tag fragments agree."""
    if "ca-verify-v1" not in tags:
        return None
    first = single_ca_tag_value(tags, "ca-vhash1-")
    second = single_ca_tag_value(tags, "ca-vhash2-")
    digest = (first or "") + (second or "")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
        return None
    return digest

def helper_swap_info(c, ip):
    """Find the separately attached local swap without trusting sdX order."""
    resolver = r'''set -Eeuo pipefail
disk_name() {
  local device="$1" parent
  parent=$(lsblk -dno PKNAME "$device" 2>/dev/null || true)
  [ -n "$parent" ] && { echo "$parent"; return; }
  basename "$device" | sed 's/[0-9]*$//'
}
root_disk=$(disk_name "$(findmnt -n -o SOURCE /)")
volume_disks=()
for link in /dev/disk/by-id/scsi-0Linode_Volume_*; do
  [ -e "$link" ] || continue
  case "$link" in *-part*) continue;; esac
  volume_disks+=("$(disk_name "$(readlink -f "$link")")")
done
is_volume_disk() { local candidate="$1" disk; for disk in "${volume_disks[@]}"; do [ "$candidate" = "$disk" ] && return 0; done; return 1; }
matches=()
while read -r name type; do
  [ "$type" = disk ] || continue
  [ "$name" = "$root_disk" ] && continue
  is_volume_disk "$name" && continue
  [ "$(blkid -s TYPE -o value "/dev/$name" 2>/dev/null || true)" = swap ] && matches+=("$name")
done < <(lsblk -dn -o NAME,TYPE)
[ "${#matches[@]}" -eq 1 ] || { echo "expected exactly one non-BSV local swap disk; found ${#matches[@]}" >&2; exit 1; }
uuid=$(blkid -s UUID -o value "/dev/${matches[0]}")
[ -n "$uuid" ] || { echo "swap UUID is empty" >&2; exit 1; }
printf '{"device":"/dev/%s","uuid":"%s"}\n' "${matches[0]}" "$uuid"'''
    end, result = time.time() + 120, None
    while time.time() < end:
        result = ssh(c, ip, f"/bin/bash -c {shlex.quote(resolver)}")
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                if data.get("uuid") and data.get("device"):
                    return data
            except json.JSONDecodeError:
                pass
        time.sleep(3)
    detail = "" if result is None else (result.stderr.strip() or result.stdout.strip())
    raise RuntimeError(f"Could not dynamically read the separate swap UUID after SSH was ready: {detail}")

def helper_swap_uuid(c, ip):
    return helper_swap_info(c, ip)["uuid"]

def resize_detached_ext4(c, ip, device, target_mb=None):
    """Check and resize a detached whole-device ext4 filesystem over the helper.

    ``target_mb`` shrinks to an intentionally smaller filesystem. ``None``
    expands it to use the current device size.
    """
    target_arg = "" if target_mb is None else f"{int(target_mb)}M"
    script = r'''set -Eeuo pipefail
device="$1"
target="$2"
[ -b "$device" ] || { echo "not a block device: $device" >&2; exit 10; }
[ "$(blkid -s TYPE -o value "$device" 2>/dev/null || true)" = ext4 ] || { echo "not ext4: $device" >&2; exit 11; }
children=$(lsblk -nr -o NAME "$device" | sed '1d' | wc -l)
[ "$children" -eq 0 ] || { echo "refusing ext4 resize: device has child block devices" >&2; exit 12; }
mounted=$(findmnt -rn -S "$device" 2>/dev/null || true)
[ -z "$mounted" ] || { echo "refusing ext4 resize: device is mounted" >&2; exit 13; }
set +e
e2fsck -fp "$device"
check_status=$?
set -e
if (( check_status & (4 | 8 | 16 | 32 | 128) )); then
  echo "e2fsck failed with status $check_status" >&2
  exit 14
fi
if [ -n "$target" ]; then resize2fs "$device" "$target"; else resize2fs "$device"; fi
size=$(blockdev --getsize64 "$device")
printf '{"ok":true,"device":"%s","raw_bytes":%s,"e2fsck_status":%s}\n' "$device" "$size" "$check_status"'''
    # e2fsck can take longer than the ordinary short SSH probe timeout. Keep
    # this control connection alive for the full offline filesystem operation.
    result = ssh(c, ip, f"/bin/bash -c {shlex.quote(script)} -- {shlex.quote(device)} {shlex.quote(target_arg)}", timeout=1800)
    if result.returncode:
        raise RuntimeError("Detached ext4 resize failed: " + (result.stderr.strip() or result.stdout.strip()))
    try:
        # e2fsck may emit a normal filesystem summary on stdout before the
        # machine-readable completion record. The final JSON line is ours.
        json_line = next((line for line in reversed(result.stdout.splitlines()) if line.lstrip().startswith("{")), "")
        return json.loads(json_line)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Detached ext4 resize returned invalid JSON: " + result.stdout.strip()) from exc

def print_matching_hashes(result, source_label, destination_label):
    source_hash = result.get("source_sha256", result.get("sha256"))
    destination_hash = result.get("destination_sha256", result.get("sha256"))
    milestone("Checksums match")
    print(f"SHA-256 {source_label}: {source_hash}")
    print(f"SHA-256 {destination_label}: {destination_hash}")

def compact_resize_sizes(inspection):
    """Return safe API/filesystem/BSV sizes from a successful inspection."""
    minimum_mb = math.ceil(int(inspection["ext4_minimum_bytes"]) / (1024 * 1024))
    calculated_allocation_mb = math.ceil((minimum_mb + RESIZE_SAFETY_BUFFER_MB) / 1024) * 1024
    compact_api_mb = max(MINIMUM_BSV_SIZE_GB * 1024, calculated_allocation_mb)
    compact_fs_mb = compact_api_mb - COMPACT_RAW_DEVICE_ALLOWANCE_MB
    if compact_fs_mb < minimum_mb + RESIZE_SAFETY_BUFFER_MB:
        raise RuntimeError("The compact local-disk allocation cannot hold the ext4 minimum plus safety buffer.")
    return {
        "ext4_minimum_mb": minimum_mb,
        "calculated_compact_root_mb": calculated_allocation_mb,
        "compact_api_mb": compact_api_mb,
        "compact_filesystem_mb": compact_fs_mb,
        "archive_bsv_gb": compact_api_mb // 1024,
    }

def reexpand_source_root(api, c, node_id, root, helper, swap_source=None, baseline=None):
    """Restore the source's original local allocation and expand ext4 to fill it."""
    shutdown(api, node_id)
    disk = api.get(f"/linode/instances/{node_id}/disks/{root['id']}")
    if int(disk["size"]) != int(root["size"]):
        resize_disk_when_ready(api, node_id, root["id"], root["size"], "source root disk back to its original allocation")
    config = create_config(
        api, node_id, "cold-archive-v2-source-reexpand",
        {"volume_id": helper["id"]}, {"disk_id": root["id"]},
        {"disk_id": swap_source["disk"]["id"]} if swap_source else None,
        kernel="linode/grub2",
    )
    try:
        milestone("Booting reusable helper to re-expand the source filesystem")
        boot(api, node_id, config["id"], "source filesystem re-expansion")
        node = api.get(f"/linode/instances/{node_id}")
        wait_ssh(c, node["ipv4"][0], "source re-expansion helper")
        inspection = inspect_resize_candidate(c, node["ipv4"][0], helper["label"])
        validate_resize_root(
            inspection, root["size"], "source re-expansion root check",
            expected_uuid=(baseline or {}).get("ext4_uuid"),
        )
        resize_detached_ext4(c, node["ipv4"][0], inspection["source_device"])
        post_expansion = inspect_resize_candidate(c, node["ipv4"][0], helper["label"])
        validate_resize_root(
            post_expansion, root["size"], "source re-expansion postcondition",
            expected_uuid=(baseline or {}).get("ext4_uuid"),
        )
        if int(post_expansion["ext4_filesystem_bytes"]) < int(post_expansion["raw_bytes"]):
            raise RuntimeError("Source re-expansion postcondition failed: ext4 does not fill the restored local disk.")
        milestone("Source ext4 filesystem re-expanded to its original local disk")
    finally:
        try: shutdown(api, node_id)
        finally:
            try: detach_volume_and_wait(api, helper['id'], "helper BSV")
            finally: delete_when_linode_ready(api, f"/linode/instances/{node_id}/configs/{config['id']}", "remove the source re-expansion configuration")

def recover_resize_source(api, c):
    """Recovery path: re-expand a source after an interrupted compact run."""
    node = api.get(f"/linode/instances/{c['source_linode_id']}")
    boot_config = source_boot_config(api, c, node["id"])
    root = source_root_disk(api, c, node["id"], boot_config)
    swap_source = source_swap_disk(api, node["id"], boot_config, root)
    print(
        "This beta recovery will power off the source VM, boot the reusable helper, "
        f"and re-expand a supported detached ext4 root to its current {root['size']} MB local disk. "
        "It will not create an archive volume. The source VM will remain powered off when complete.",
        flush=True,
    )
    require_yes(f"RECOVER ROOT {node['id']}")
    helper = helper_boot_volume(api, c, node["region"])
    reexpand_source_root(api, c, node["id"], root, helper, swap_source)
    print("Source root recovery completed; its ext4 filesystem now fills the original local disk and the VM remains powered off.")

def archive_bsv_resize_min(api, c):
    """Compact ext4 archive with automatic source recovery before verification."""
    milestone("Version 2.0 compact ext4 archive started")
    node = api.get(f"/linode/instances/{c['source_linode_id']}")
    boot_config = source_boot_config(api, c, node["id"])
    root = source_root_disk(api, c, node["id"], boot_config)
    swap_source = source_swap_disk(api, node["id"], boot_config, root)
    _, attached_volumes, extra_local = source_inventory(api, node["id"], boot_config, root, swap_source)
    if extra_local:
        raise RuntimeError("Version 2.0 --resize min supports only a root local disk and optional separate swap; remove extra local disks before running it.")
    if not c.get("archive_label"):
        c["archive_label"] = next_archive_label(api, node["label"])
        milestone(f"No archive name supplied; using {c['archive_label']}")
    validate_volume_label(c["archive_label"])
    existing = [v for v in api.get("/volumes?page=1&page_size=100")["data"] if v.get("region") == node["region"] and v.get("label") == c["archive_label"]]
    if existing:
        raise RuntimeError(f"An archive volume named {c['archive_label']!r} already exists in {node['region']}; choose another --archive-name.")
    print(
        "This beta workflow will temporarily shrink the detached whole-device ext4 root filesystem, "
        "then archive it.\n"
        "It is limited to a root local disk with an optional separate swap disk.\n\n"
        f"Source: {node['label']} (Linode {node['id']})\n"
        f"Original root allocation: {root['size']} MB\n"
        f"Requested resize mode: min (ext4 minimum + {RESIZE_SAFETY_BUFFER_MB} MB buffer; provider BSV minimum {MINIMUM_BSV_SIZE_GB} GB)\n",
        flush=True,
    )
    if c.get("delete_source"):
        print(
            "After the archive has fully verified, the source VM will be deleted only after a final exact confirmation.\n"
            "Until then, any failure automatically restores the source disk and filesystem to their original size.",
            flush=True,
        )
    if attached_volumes:
        print("Attached Block Storage volumes will remain attached but are excluded from this root-disk archive:", flush=True)
        for slot, label, size in attached_volumes:
            suffix = f" ({size} GB)" if size is not None else ""
            print(f"  - /dev/{slot}: {label}{suffix}", flush=True)
    helper = helper_boot_volume(api, c, node["region"])
    initial_config = None
    copy_config = None
    archive_volume = None
    archive_finalized = False
    source_touched = False
    source_reexpanded = False
    source_deleted = False
    inspection = None
    try:
        shutdown(api, node["id"])
        initial_config = create_config(
            api, node["id"], "cold-archive-v2-resize-inspect",
            {"volume_id": helper["id"]}, {"disk_id": root["id"]},
            {"disk_id": swap_source["disk"]["id"]} if swap_source else None,
            kernel="linode/grub2",
        )
        milestone("Booting reusable helper for final read-only ext4 qualification")
        boot(api, node["id"], initial_config["id"], "resize qualification")
        node = api.get(f"/linode/instances/{node['id']}")
        wait_ssh(c, node["ipv4"][0], "resize qualification helper")
        inspection = inspect_resize_candidate(c, node["ipv4"][0], helper["label"])
        if not inspection.get("supported"):
            reason = inspection.get("reason", "the root filesystem/layout is unsupported")
            milestone("Minimum-size archive is unavailable; ending read-only resize inspection")
            shutdown(api, node["id"])
            detach_volume_and_wait(api, helper["id"], "helper BSV")
            delete_when_linode_ready(
                api,
                f"/linode/instances/{node['id']}/configs/{initial_config['id']}",
                "remove the resize qualification configuration",
            )
            initial_config = None
            print(
                "A minimum-size archive is unavailable for this source.\n"
                f"Reason: {reason}.\n"
                "This beta shrinks only a proven whole-device ext4 Linux root; Windows/NTFS and partitioned roots are intentionally not modified.\n"
                "The source disk and filesystem were not changed.",
                flush=True,
            )
            answer = input("Continue with an original-size archive instead? [Y/n]: ").strip().lower()
            if answer not in ("", "y", "yes"):
                print("No archive created. The source VM remains powered off.", flush=True)
                return
            milestone("Continuing with the normal original-size archive; no source resize will be performed")
            c["resize_mode"] = "original"
            archive_bsv(api, c)
            return
        validate_resize_root(inspection, root["size"], "read-only source qualification")
        sizes = compact_resize_sizes(inspection)
        print(json.dumps({"filesystem_minimum_mb": sizes["ext4_minimum_mb"], "safety_buffer_mb": RESIZE_SAFETY_BUFFER_MB,
                          "compact_source_disk_mb": sizes["compact_api_mb"], "compact_ext4_mb": sizes["compact_filesystem_mb"],
                          "archive_bsv_gb": sizes["archive_bsv_gb"], "original_source_disk_mb": root["size"]}, indent=2))
        source_after_verification = (
            "delete the source VM after a final exact confirmation"
            if c.get("delete_source")
            else "restore the source disk and filesystem to their original size"
        )
        print(
            f"This will now shrink the source root to {sizes['compact_api_mb']} MB, create the "
            f"{sizes['archive_bsv_gb']} GB archive BSV {c['archive_label']!r}, then {source_after_verification}.",
            flush=True,
        )
        require_yes(f"COMPACT ARCHIVE {node['id']} AS {c['archive_label']} TO {sizes['compact_api_mb']} MB")
        milestone("Checking and shrinking detached source ext4 filesystem")
        # e2fsck may repair metadata before resize2fs returns; from this point
        # forward an error path must run the source re-expansion safeguard.
        source_touched = True
        resize_detached_ext4(c, node["ipv4"][0], inspection["source_device"], sizes["compact_filesystem_mb"])
        post_shrink = inspect_resize_candidate(c, node["ipv4"][0], helper["label"])
        validate_resize_root(
            post_shrink, root["size"], "post-shrink source check",
            expected_uuid=inspection["ext4_uuid"],
            maximum_filesystem_bytes=sizes["compact_filesystem_mb"] * 1024 * 1024,
        )
        shutdown(api, node["id"])
        detach_volume_and_wait(api, helper['id'], "helper BSV")
        delete_when_linode_ready(api, f"/linode/instances/{node['id']}/configs/{initial_config['id']}", "remove the resize qualification configuration")
        initial_config = None
        resize_disk_when_ready(api, node["id"], root["id"], sizes["compact_api_mb"], "source root disk for compact archive")
        milestone(f"Creating compact {sizes['archive_bsv_gb']} GB archive Block Storage volume")
        archive_volume = api.post("/volumes", {"label": c["archive_label"], "region": node["region"], "size": sizes["archive_bsv_gb"], "tags": ["ca-archive-building-v2"]})
        copy_config = create_config(api, node["id"], "cold-archive-v2-compact-copy", {"volume_id": helper["id"]}, {"disk_id": root["id"]}, {"volume_id": archive_volume["id"]}, {"disk_id": swap_source["disk"]["id"]} if swap_source else None, kernel="linode/grub2")
        milestone("Booting source from reusable Block Storage helper for compact archive copy")
        boot(api, node["id"], copy_config["id"], "compact archive helper configuration")
        node = api.get(f"/linode/instances/{node['id']}")
        wait_ssh(c, node["ipv4"][0], "compact archive helper")
        compact_inspection = inspect_resize_candidate(c, node["ipv4"][0], helper["label"])
        validate_resize_root(
            compact_inspection, sizes["compact_api_mb"], "compact source-disk check",
            expected_uuid=inspection["ext4_uuid"],
            maximum_filesystem_bytes=sizes["compact_filesystem_mb"] * 1024 * 1024,
        )
        swap = None
        if swap_source:
            swap = {"size_mb": swap_source["disk"]["size"], "slot": swap_source["slot"], "label": swap_source["disk"]["label"], "uuid": helper_swap_uuid(c, node["ipv4"][0])}
            milestone(f"Recorded separate swap disk: {swap['size_mb']} MB at {swap['slot']}")
        start_bsv_worker(c, node["ipv4"][0], "archive", int(compact_inspection["raw_bytes"]), archive_volume["label"])
        milestone("Compact archive copy worker started; device discovery may be quiet briefly before the first percentage")
        result = helper_result(c, node["ipv4"][0])
        print_matching_hashes(result, "compact local source disk", "compact archive BSV")
        milestone("Compact copy and full checksum completed successfully")
        shutdown(api, node["id"])
        detach_volume_and_wait(api, archive_volume['id'], "compact archive BSV")
        detach_volume_and_wait(api, helper['id'], "helper BSV")
        delete_when_linode_ready(api, f"/linode/instances/{node['id']}/configs/{copy_config['id']}", "remove the compact archive configuration")
        copy_config = None
        resize_metadata = {"mode": "min", "filesystem": "ext4", "layout": "whole-device", "original_api_mb": root["size"], "compact_api_mb": sizes["compact_api_mb"], "safety_buffer_mb": RESIZE_SAFETY_BUFFER_MB}
        source_bytes = int(result["source_bytes"])
        api.put(f"/volumes/{archive_volume['id']}", {"tags": archive_tags(node, source_bytes, root["size"], boot_config, result["sha256"], swap, resize_metadata)})
        archive_finalized = True
        if c.get("delete_source"):
            require_yes(f"DELETE LINODE {node['id']}")
            delete_when_linode_ready(api, f"/linode/instances/{node['id']}", "delete the verified compact-archive source VM")
            source_deleted = True
            print("Source Linode deleted; the verified compact archive Block Storage volume remains.")
        else:
            milestone("Restoring source root disk and filesystem to their original size")
            reexpand_source_root(api, c, node["id"], root, helper, swap_source, inspection)
            source_reexpanded = True
            print("Compact archive completed and the source VM has been restored to its original disk allocation; it remains powered off.")
    except Exception as exc:
        if source_touched and not source_reexpanded and not source_deleted:
            milestone("Workflow failed after source filesystem shrink; automatically restoring the source to its original size")
            # Release any copy/inspection configuration before attaching the
            # reusable helper to the dedicated re-expansion configuration.
            try: shutdown(api, node["id"])
            except Exception: pass
            for volume in (archive_volume, helper):
                if volume:
                    try: detach_volume_and_wait(api, volume['id'], "temporary BSV")
                    except Exception: pass
            for config in (copy_config, initial_config):
                if config:
                    try: delete_when_linode_ready(api, f"/linode/instances/{node['id']}/configs/{config['id']}", "remove a failed compact-archive configuration")
                    except Exception: pass
            copy_config = initial_config = None
            try:
                reexpand_source_root(api, c, node["id"], root, helper, swap_source, inspection)
                source_reexpanded = True
                raise RuntimeError(f"{exc} Source disk and filesystem were restored to their original size.") from exc
            except RuntimeError as recovery_exc:
                if str(recovery_exc).startswith(str(exc)):
                    raise
                raise RuntimeError(f"{exc} CRITICAL: automatic source recovery also failed: {recovery_exc}") from exc
        raise
    finally:
        for config in (copy_config, initial_config):
            if config:
                try: delete_when_linode_ready(api, f"/linode/instances/{node['id']}/configs/{config['id']}", "remove a temporary compact-archive configuration")
                except Exception: pass
        if not archive_finalized:
            for volume in (archive_volume, helper):
                if volume:
                    try: detach_volume_and_wait(api, volume['id'], "temporary BSV")
                    except Exception: pass
            if archive_volume:
                try:
                    api.delete(f"/volumes/{archive_volume['id']}")
                    milestone("Removed the unverified compact archive BSV created by the failed workflow")
                except Exception:
                    pass

def archive_bsv(api, c):
    """Archive: boot source from the BSV helper; never resize its plan."""
    milestone("Version 2.0 archive started")
    node = api.get(f"/linode/instances/{c['source_linode_id']}")
    boot_config = source_boot_config(api, c, node["id"])
    root = source_root_disk(api, c, node["id"], boot_config)
    swap_source = source_swap_disk(api, node["id"], boot_config, root)
    if not c.get("archive_label"):
        c["archive_label"] = next_archive_label(api, node["label"])
        milestone(f"No archive name supplied; using {c['archive_label']}")
    validate_volume_label(c["archive_label"])
    duplicate_archive = [v for v in api.get("/volumes?page=1&page_size=100")["data"]
                         if v.get("region") == node["region"] and v.get("label") == c["archive_label"]]
    if duplicate_archive:
        raise RuntimeError(f"An archive volume named {c['archive_label']!r} already exists in {node['region']}; choose another --archive-name.")
    archive_gb = math.ceil(root["size"] / 1024)
    source_outcome = (
        "The source VM will be powered off before copying, then deleted only after verification and a final confirmation."
        if c.get("delete_source")
        else "The source VM will be powered off before copying and will remain powered off when complete."
    )
    root_slot, attached_volumes, extra_local = source_inventory(api, node["id"], boot_config, root, swap_source)
    preserved = [f"  - Root local disk /dev/{root_slot}: {root['label']} ({root['size']} MB), raw-copied"]
    if swap_source:
        swap_disk = swap_source["disk"]
        preserved.append(f"  - Swap disk /dev/{swap_source['slot']}: {swap_disk['label']} ({swap_disk['size']} MB), metadata captured and recreated blank on restore")
    excluded = []
    for slot, label, size in attached_volumes:
        suffix = f" ({size} GB)" if size is not None else ""
        excluded.append(f"  - Attached Block Storage volume /dev/{slot}: {label}{suffix}")
    for slot, label, size, filesystem in extra_local:
        filesystem_note = f", filesystem {filesystem}" if filesystem else ""
        excluded.append(f"  - Additional local disk /dev/{slot}: {label} ({size} MB{filesystem_note})")
    inventory = "Will preserve:\n" + "\n".join(preserved)
    if excluded:
        inventory += "\n\nNot included in this root-disk archive:\n" + "\n".join(excluded)
    print(
        f"This will create a {archive_gb} GB archive Block Storage volume in {node['region']}:\n"
        f"  {c['archive_label']}\n\n"
        f"It will measure the source device's true byte length after helper boot, then copy and fully checksum-verify it.\n"
        f"The API reports the root disk as {root['size']} MiB; the archive BSV is sized safely from that value.\n"
        f"{source_outcome}\n\n{inventory}",
        flush=True,
    )
    require_yes(f"ARCHIVE {node['id']} AS {c['archive_label']}")
    helper = helper_boot_volume(api, c, node["region"])
    shutdown(api, node["id"])
    milestone("Creating archive Block Storage volume")
    archive_volume = api.post("/volumes", {"label": c["archive_label"], "region": node["region"], "size": archive_gb, "tags": ["ca-archive-building-v1"]})
    config = create_config(api, node["id"], "cold-archive-bsv-copy", {"volume_id": helper["id"]}, {"disk_id": root["id"]}, {"volume_id": archive_volume["id"]}, {"disk_id": swap_source["disk"]["id"]} if swap_source else None, kernel="linode/grub2")
    ACTIVE_CLEANUPS.append(lambda: cleanup_bsv_session(api, node["id"], config["id"], [archive_volume["id"], helper["id"]]))
    milestone("Booting source from reusable Block Storage helper")
    boot(api, node["id"], config["id"], "archive helper configuration")
    node = api.get(f"/linode/instances/{node['id']}")
    wait_ssh(c, node["ipv4"][0], "archive helper")
    milestone("Archive helper is SSH-ready; starting copy worker")
    swap = None
    if swap_source:
        swap = {"size_mb": swap_source["disk"]["size"], "slot": swap_source["slot"], "label": swap_source["disk"]["label"], "uuid": helper_swap_uuid(c, node["ipv4"][0])}
        milestone(f"Recorded separate swap disk: {swap['size_mb']} MB at {swap['slot']}")
    # The API's disk size is a provisioning value, not always the raw device
    # length.  Zero tells the helper to measure the local source block device.
    start_bsv_worker(c, node["ipv4"][0], "archive", 0, archive_volume["label"])
    milestone("Copy worker started; device discovery may be quiet briefly before the first percentage")
    result = helper_result(c, node["ipv4"][0])
    print_matching_hashes(result, "local source disk", "archive BSV")
    milestone("Copy and full checksum completed successfully")
    shutdown(api, node["id"])
    api.post(f"/volumes/{archive_volume['id']}/detach", {})
    api.post(f"/volumes/{helper['id']}/detach", {})
    api.delete(f"/linode/instances/{node['id']}/configs/{config['id']}")
    ACTIVE_CLEANUPS.clear()
    source_bytes = int(result["source_bytes"])
    source_mb = math.ceil(source_bytes / (1024 * 1024))
    api.put(f"/volumes/{archive_volume['id']}", {"tags": archive_tags(node, source_bytes, root["size"], boot_config, result["sha256"], swap)})
    archive_log = {"archive_format": "ca-format-1", "created_at": datetime.now(timezone.utc).isoformat(), "volume_id": archive_volume["id"], "region": node["region"], "source_disk_mb": source_mb, "source_api_disk_mb": root["size"], "source_bytes": source_bytes, "source_plan": node["type"], "source_label": node["label"], "source_linode_id": node["id"], "source_kernel": boot_config["kernel"], "source_root_device": boot_config.get("root_device", "/dev/sda"), "swap": swap, "sha256": result["sha256"], "helper_boot_volume_id": helper["id"]}
    path = out_dir(c) / f"{c['archive_label']}.json"; path.write_text(json.dumps(archive_log, indent=2), encoding="utf-8")
    print(f"Archive log written: {path}")
    if c.get("delete_source"):
        require_yes(f"DELETE LINODE {node['id']}"); api.delete(f"/linode/instances/{node['id']}")
        print("Source Linode deleted; the archive Block Storage volume remains.")
    else:
        print("The source VM is powered off.")

def verify_archive_volume(api, m):
    volume = api.get(f"/volumes/{m['volume_id']}")
    if volume.get("linode_id") is not None:
        raise RuntimeError(f"Archive volume {m['volume_id']} is attached to Linode {volume['linode_id']}; detach it before restore.")
    if volume.get("status") not in ("available", "active"):
        raise RuntimeError(f"Archive volume {m['volume_id']} is not ready (status: {volume.get('status')}).")
    return volume

def archive_metadata_from_volume(api, volume_id):
    """Recovery metadata for an operator who has only the tagged volume."""
    volume = api.get(f"/volumes/{volume_id}")
    tags = list(volume.get("tags") or [])
    validate_archive_metadata_tags(tags)
    archive_format = single_ca_tag_value(tags, "ca-format-")
    if archive_format not in ("1", "2b1"):
        raise RuntimeError("This controller accepts only ca-format-1 or Version 2 beta ca-format-2b1 archive volumes.")
    def tagged(prefix):
        return single_ca_tag_value(tags, prefix)
    source_mb = tagged("ca-source-disk-mb-")
    source_api_mb = tagged("ca-root-api-mb-")
    source_bytes = tagged("ca-source-bytes-")
    if not source_mb or not source_api_mb or not source_bytes:
        raise RuntimeError("Archive volume is missing its exact source-size tags.")
    source_mb = int(source_mb)
    swap_mb, swap_slot, swap_uuid = tagged("ca-swap1-mb-"), tagged("ca-swap1-slot-"), tagged("ca-swap1-uuid-")
    swap = None
    if any((swap_mb, swap_slot, swap_uuid)):
        if not all((swap_mb, swap_slot, swap_uuid)):
            raise RuntimeError("Archive volume has incomplete separate-swap metadata tags.")
        swap = {"size_mb": int(swap_mb), "slot": swap_slot, "label": "swap", "uuid": swap_uuid}
    kernel = tagged("ca-boot-kernel-")
    root_device = tagged("ca-boot-root-")
    if not kernel or not root_device:
        raise RuntimeError("Archive volume is missing source boot-configuration tags.")
    verified_hash = verified_hash_from_tags(tags)
    source_label = tagged("ca-source-label-")
    resize = None
    if archive_format == "2b1":
        resize_mode = tagged("ca-resize-mode-")
        resize_fs = tagged("ca-resize-fs-")
        resize_layout = tagged("ca-resize-layout-")
        original_mb = tagged("ca-resize-original-mb-")
        compact_mb = tagged("ca-resize-compact-mb-")
        buffer_mb = tagged("ca-resize-buffer-mb-")
        if "ca-resize-v1" not in tags or not all((resize_mode, resize_fs, resize_layout, original_mb, compact_mb, buffer_mb)):
            raise RuntimeError("Version 2 beta archive has incomplete resize metadata tags.")
        resize = {"mode": resize_mode, "filesystem": resize_fs, "layout": resize_layout,
                  "original_api_mb": int(original_mb), "compact_api_mb": int(compact_mb),
                  "safety_buffer_mb": int(buffer_mb)}
        if not source_label:
            label_one, label_two = tagged("ca-source-label1-"), tagged("ca-source-label2-")
            if not label_one or label_two is None:
                raise RuntimeError("Version 2 beta archive is missing complete source-label metadata tags.")
            source_label = label_one + label_two
    if not source_label:
        raise RuntimeError("Archive volume is missing its source-label tag.")
    return {"volume_id": volume["id"], "region": volume["region"], "source_api_disk_mb": int(source_api_mb), "source_bytes": int(source_bytes), "source_label": source_label, "source_plan": tagged("ca-source-plan-"), "source_kernel": f"linode/{kernel}", "source_root_device": f"/dev/{root_device}", "swap": swap, "sha256": verified_hash, "archive_format": archive_format, "resize": resize}
def archive_volume_by_label(api, label):
    """Find one current tagged archive by its Cloud Manager volume label."""
    matches, page = [], 1
    while True:
        response = api.get(f"/volumes?page={page}&page_size=100")
        matches.extend(volume for volume in response.get("data", [])
                       if volume.get("label") == label and ({"ca-format-1", "ca-format-2b1"} & set(volume.get("tags") or [])))
        if page >= response.get("pages", 1):
            break
        page += 1
    if len(matches) > 1:
        ids = ", ".join(str(volume["id"]) for volume in matches)
        raise RuntimeError(f"More than one tagged archive volume is named {label!r} (IDs: {ids}). Restore with --volume-id.")
    return matches[0] if matches else None

def load_archive_metadata(api, c):
    if c.get("archive_volume_id"):
        return archive_metadata_from_volume(api, int(c["archive_volume_id"]))
    # --archive-name means the current Cloud Manager volume label. The local
    # archive log is deliberately not a recovery input.
    if c.get("archive_lookup_label"):
        volume = archive_volume_by_label(api, c["archive_lookup_label"])
        if volume:
            return archive_metadata_from_volume(api, volume["id"])
        raise RuntimeError(
            f"No tagged compatible archive BSV named {c['archive_lookup_label']!r} was found. "
            "Restore requires --archive-name for the current BSV label or --volume-id; the local archive log is not used for recovery."
        )
    raise RuntimeError("Restore requires --archive-name for the current BSV label or --volume-id.")

def restore_plan(api, c, m):
    volume = verify_archive_volume(api, m)
    target_mb = math.ceil(int(m["source_bytes"]) / (1024 * 1024))
    choose_restore_disk_size(c, m)
    p = plan(api, c["restore_plan"])
    requested_target_mb = choose_custom_restore_size(c, m, p)
    swap_mb = int((m.get("swap") or {}).get("size_mb", 0))
    required_plan_mb = requested_target_mb + swap_mb
    compact_floor = int((m.get("resize") or {}).get("compact_api_mb", 0))
    if requested_target_mb < (compact_floor or minimum_restore_allocation_mb(m)):
        raise RuntimeError("Requested restore allocation cannot be smaller than the compact archive allocation.")
    eligible, _sources, _account_filtered = restore_candidates(api, volume["region"], required_plan_mb)
    if not any(candidate["id"] == p["id"] for candidate in eligible):
        raise RuntimeError(
            f"Plan {p['id']} is unavailable in {volume['region']} or cannot hold the "
            f"requested {requested_target_mb} MB restored root plus {swap_mb} MB swap. Choose another plan."
        )
    print(json.dumps({"archive_volume_id":m["volume_id"],"archive_volume_region":volume["region"],"final_restore_plan":p["id"],"final_plan_local_disk_mb":p["disk"],"restored_data_mb":target_mb,"requested_restore_disk_mb":requested_target_mb,"restored_swap_mb":swap_mb,"required_plan_local_disk_mb":required_plan_mb,"local_disk_cushion_mb":LOCAL_DISK_CUSHION_MB,"final_plan_fits":p["disk"] >= required_plan_mb}, indent=2))
    if p["disk"] < required_plan_mb: raise RuntimeError("Final restore plan cannot hold the requested restored root disk and swap disk.")

def restore_bsv(api, c):
    """Restore directly on the final plan and boot only the BSV helper."""
    milestone("Version 2.0 restore started")
    m = load_archive_metadata(api, c)
    choose_restore_disk_size(c, m)
    if m.get("source_plan") and not c.get("restore_plan_explicit"):
        c["restore_plan"] = m["source_plan"]
    target_mb = math.ceil(int(m["source_bytes"]) / (1024 * 1024))
    compact_floor = int((m.get("resize") or {}).get("compact_api_mb", 0))
    validate_linode_label(c["restore_label"])
    # Build or validate the regional helper before creating a billable restored
    # VM. A helper-build failure must not leave an otherwise empty target VM.
    helper = helper_boot_volume(api, c, verify_archive_volume(api, m)["region"])
    unavailable_plan_ids = set()
    while True:
        volume = choose_restore_plan(api, c, m, unavailable_plan_ids)
        final = plan(api, c["restore_plan"])
        requested_target_mb = choose_custom_restore_size(c, m, final)
        swap_mb = int((m.get("swap") or {}).get("size_mb", 0))
        required_plan_mb = requested_target_mb + swap_mb
        if requested_target_mb < (compact_floor or minimum_restore_allocation_mb(m)):
            raise RuntimeError("Requested restore allocation cannot be smaller than the compact archive allocation.")
        if final["disk"] < required_plan_mb:
            raise RuntimeError(f"Final plan {final['id']} cannot hold the {requested_target_mb} MB restored root allocation plus {swap_mb} MB swap.")
        print(
            f"This will create the restored VM in {volume['region']}:\n"
            f"  Name: {c['restore_label']}\n"
            f"  Plan: {final['id']} ({price_text(final, volume['region'])})\n"
            f"  Local boot disk: {requested_target_mb} MB\n"
            + (f"  Local swap disk: {swap_mb} MB\n" if swap_mb else "")
            + f"  Total local allocation: {required_plan_mb} MB\n"
            f"  Source archive volume: {m['volume_id']} ({volume['size']} GB)",
            flush=True,
        )
        require_yes(f"RESTORE {m['volume_id']} AS {c['restore_label']}")
        milestone(f"Creating the restored Linode directly as {final['id']}")
        created_at = datetime.now(timezone.utc)
        try:
            node = api.post("/linode/instances", {"region": volume["region"], "type": final["id"], "label": c["restore_label"]})
            break
        except RuntimeError as exc:
            unavailable = "not currently available in the selected region" in str(exc).lower()
            if c.get("restore_plan_explicit") or not unavailable:
                raise
            unavailable_plan_ids.add(final["id"])
            c["restore_plan"] = None
            print(
                f"Plan {final['id']} is not currently available in {volume['region']}; it has been removed from this selection list. Choose another plan.",
                flush=True,
            )
    node = wait_for_linode_create(api, node["id"], created_at, "restored VM")
    milestone("Creating restored local disk; no local helper disk is required")
    target = post_when_linode_ready(api, f"/linode/instances/{node['id']}/disks", {"label": "boot-disk", "size": requested_target_mb}, "create the restored local disk")
    swap = m.get("swap")
    if swap and swap["slot"] != "sdb":
        raise RuntimeError(f"Version 1.0 supports a separate swap disk only in sdb; archive records {swap['slot']}.")
    swap_disk = None
    if swap:
        milestone(f"Creating blank {swap['size_mb']} MB restored swap disk")
        swap_disk = post_when_linode_ready(api, f"/linode/instances/{node['id']}/disks", {"label": swap.get("label") or "swap", "size": swap["size_mb"], "filesystem": "swap"}, "create the restored swap disk")
    config = create_config(api, node["id"], "cold-restore-bsv-copy", {"volume_id": helper["id"]}, {"disk_id": target["id"]}, {"volume_id": m["volume_id"]}, {"disk_id": swap_disk["id"]} if swap_disk else None, kernel="linode/grub2")
    ACTIVE_CLEANUPS.append(lambda: cleanup_bsv_session(api, node["id"], config["id"], [m["volume_id"], helper["id"]]))
    milestone("Booting reusable Block Storage helper on the final plan")
    boot(api, node["id"], config["id"], "restore helper configuration")
    node = api.get(f"/linode/instances/{node['id']}")
    milestone("Helper booted; starting copy worker")
    volume_tags = list(volume.get("tags") or [])
    tagged_hash = verified_hash_from_tags(volume_tags)
    trusted_hash = m.get("sha256") if (c.get("restore_verification", "auto") == "auto" and tagged_hash == m.get("sha256")) else None
    mode = "restore_auto" if trusted_hash else "restore"
    if trusted_hash:
        milestone("Restore verification: using the archive's verified checksum tag; hashing restored local disk only")
    elif c.get("restore_verification") == "full":
        milestone("Restore verification: full archive-BSV and restored-local-disk checksum requested by --restore-verification full")
    elif "ca-verify-v1" in volume_tags:
        milestone("Archive verified-checksum tags are incomplete or invalid; performing a full archive-BSV and restored-local-disk checksum")
    else:
        milestone("Archive has no verified checksum tags; performing a full archive-BSV and restored-local-disk checksum")
    start_bsv_worker(c, node["ipv4"][0], mode, int(m["source_bytes"]), volume["label"])
    milestone("Copy worker started; device discovery may be quiet briefly before the first percentage")
    result = helper_result(c, node["ipv4"][0], workflow=mode)
    if trusted_hash:
        if result["sha256"] != trusted_hash:
            raise RuntimeError("Restored local-disk checksum does not match the archive's verified checksum tag.")
        milestone("Restored local-disk checksum matches the archive's verified checksum tag")
        print(f"SHA-256 archive verified tag: {trusted_hash}")
        print(f"SHA-256 restored local disk: {result['sha256']}")
    else:
        print_matching_hashes(result, "archive BSV", "restored local disk")
    milestone("Restore copy and restored-local-disk checksum completed successfully" if trusted_hash else "Restore copy and full checksum completed successfully")
    if m.get("resize") and requested_target_mb > int(m["resize"]["compact_api_mb"]):
        milestone("Expanding the restored compact ext4 filesystem to the requested larger local disk")
        expansion_target = inspect_resize_candidate(c, node["ipv4"][0], helper["label"])
        if not expansion_target.get("supported"):
            raise RuntimeError("Could not rediscover the restored ext4 local disk for expansion: " + expansion_target.get("reason", "unknown reason"))
        resize_detached_ext4(c, node["ipv4"][0], expansion_target["source_device"])
        milestone("Restored ext4 filesystem expanded to fill the requested local disk")
    elif m.get("resize"):
        milestone("Keeping the restored ext4 filesystem at its compact archive size")
    if swap:
        restored_swap = helper_swap_info(c, node["ipv4"][0])
        formatted = ssh(
            c, node["ipv4"][0],
            f"mkswap -U {shlex.quote(swap['uuid'])} {shlex.quote(restored_swap['device'])}",
            timeout=120,
        )
        if formatted.returncode:
            raise RuntimeError("Could not format restored swap disk with its archived UUID: " + formatted.stderr.strip())
        milestone(f"Restored blank swap disk with original UUID at {restored_swap['device']} (configuration slot {swap['slot']})")
    shutdown(api, node["id"])
    if m.get("sha256") and result["sha256"] != m["sha256"]:
            raise RuntimeError("Restore checksum does not match the archive metadata; refusing to boot the restored disk.")
    api.post(f"/volumes/{m['volume_id']}/detach", {})
    api.post(f"/volumes/{helper['id']}/detach", {})
    bootcfg = create_config(api, node["id"], "boot-disk", {"disk_id": target["id"]}, {"disk_id": swap_disk["id"]} if swap_disk else None, kernel=m.get("source_kernel", "linode/grub2"), root_device=m.get("source_root_device", "/dev/sda"))
    api.delete(f"/linode/instances/{node['id']}/configs/{config['id']}")
    ACTIVE_CLEANUPS.clear()
    milestone("Copy helper removed; booting restored local disk on the requested final plan")
    boot(api, node["id"], bootcfg["id"], "restored local disk")
    print("Restore completed: copy and checksum verification succeeded.")
    print(f"Restored VM {node['label']} (Linode {node['id']}) is booting on {final['id']}. Public IPv4: {node['ipv4'][0]}")
    print("Please confirm that the guest OS boots, reapply firewalls as needed, and verify the restored VM's network interfaces.")

def verify_archive_bsv(api, c):
    """Verification is read-only and consumes no local helper capacity."""
    milestone("Version 2.0 read-only archive verification started")
    m = load_archive_metadata(api, c)
    archive_volume = verify_archive_volume(api, m)
    node = api.get(f"/linode/instances/{c['source_linode_id']}")
    root = source_root_disk(api, c, node["id"])
    helper = helper_boot_volume(api, c, node["region"])
    shutdown(api, node["id"])
    config = create_config(api, node["id"], "cold-archive-bsv-verify", {"volume_id": helper["id"]}, {"disk_id": root["id"]}, {"volume_id": archive_volume["id"]}, kernel="linode/grub2")
    ACTIVE_CLEANUPS.append(lambda: cleanup_bsv_session(api, node["id"], config["id"], [archive_volume["id"], helper["id"]]))
    milestone("Booting reusable Block Storage helper for read-only verification")
    boot(api, node["id"], config["id"], "read-only verification helper")
    node = api.get(f"/linode/instances/{node['id']}")
    start_bsv_worker(c, node["ipv4"][0], "verify", int(m["source_bytes"]), archive_volume["label"])
    milestone("Verification worker started; device discovery may be quiet briefly before the first percentage")
    result = helper_result(c, node["ipv4"][0], workflow="verify")
    shutdown(api, node["id"])
    api.post(f"/volumes/{archive_volume['id']}/detach", {})
    api.post(f"/volumes/{helper['id']}/detach", {})
    api.delete(f"/linode/instances/{node['id']}/configs/{config['id']}")
    ACTIVE_CLEANUPS.clear()
    print(json.dumps({"original_root_and_archive_volume_match": result["match"], "original_root_sha256": result["source_sha256"], "archive_volume_sha256": result["destination_sha256"], "archive_tag_sha256": m.get("sha256"), "archive_tag_matches_original": result["source_sha256"] == m.get("sha256")}, indent=2))
    if not result["match"]:
        print("WARNING: Archive volume differs from the preserved original root disk. Do not restore from this volume.")

def main():
    parser=argparse.ArgumentParser(
        usage="%(prog)s <command> [options]",
        description=("Archive or restore a Linode VM's local boot disk through Block Storage.\n\n"
                     "Commands:\n"
                     "  archive            Create and verify an archive Block Storage volume.\n"
                     "  restore            Create and boot a new VM from an archive.\n\n"
                     "Advanced commands:\n"
                     "  verify-archive     Compare an archive to a preserved source VM.\n"
                     "  plan-resize        Read-only qualification for a minimum-size ext4 archive.\n"
                     "  recover-resize     Re-expand a source after an interrupted minimum-size archive.\n"
                     "  prepare-bsv-helper Prebuild the reusable helper volume for a region."),
        epilog=("General options:\n"
                "  -h, --help                  Show this help message and exit.\n"
                "  --version                   Show the program version.\n"
                "  --config CONFIG_FILE        JSON configuration file and path (default: ./config.json).\n\n"
                "archive options:\n"
                "  --linode-id LINODE_ID       Source VM ID. Required unless --linode-label is used.\n"
                "  --linode-label LABEL        Exact, unique source VM label. Alternative to --linode-id.\n"
                "  --archive-name NAME         Archive Block Storage volume name; default: <source>-archive.\n"
                "  --resize {original,min}     original preserves full local-disk size (default); min compacts a\n"
                "                              supported whole-device ext4 Linux root before archiving.\n"
                "  --delete-source             Delete source only after verification and a separate exact confirmation.\n"
                "  --dry-run                   Inspect source and proposed ordinary archive sizing; no VM or BSV changes.\n\n"
                "restore options:\n"
                "  --archive-name NAME         Archive BSV name. Required unless --volume-id is used.\n"
                "  --volume-id VOLUME_ID       Archive BSV ID. Alternative to --archive-name.\n"
                "  --new-vm-name NAME          Restored VM name; default: <source>-rN.\n"
                "  --restore-plan PLAN         Restored VM plan; default: archived source plan.\n"
                "  --restore-size {original,min,custom}\n"
                "                              Compact archives only: original expands to the original root allocation\n"
                "                              (default); min retains compact size; custom prompts for MB after plan selection.\n"
                "  --restore-verification {auto,full}\n"
                "                              auto hashes restored disk against archive tag (default); full hashes both disks.\n"
                "  --dry-run                   Validate archive and explicit --restore-plan without creating a VM.\n\n"
                "Examples:\n"
                "  python3 linode_vm_disk_archive.py archive --linode-label my-vm --resize min\n"
                "      Creates my-vm-archive, using the minimum supported ext4 root size when eligible.\n"
                "  python3 linode_vm_disk_archive.py restore --archive-name my-vm-archive\n"
                "      Creates my-vm-r1 and restores a compact archive to its original root allocation.\n\n"
                "Notes:\n"
                "  archive and restore automatically create and boot-test a reusable 10 GB helper BSV when absent.\n"
                "  The archive includes the selected local root disk. One separate local swap disk is recorded and\n"
                "  recreated blank with its original UUID. Attached Block Storage and additional local disks are not copied."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False)
    parser.add_argument("command",choices=["plan-resize","recover-resize","archive","restore","verify-archive","prepare-bsv-helper"], help=argparse.SUPPRESS)
    parser.add_argument("-h", "--help", action="help", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"Linode VM Disk Archive {SCRIPT_REVISION}", help=argparse.SUPPRESS)
    parser.add_argument("--config", metavar="CONFIG_FILE", default="config.json", help=argparse.SUPPRESS)
    parser.add_argument("--linode-id", metavar="LINODE_ID", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--linode-label", metavar="LABEL", help=argparse.SUPPRESS)
    parser.add_argument("--delete-source", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--resize", choices=["original", "min"], default="original", help=argparse.SUPPRESS)
    parser.add_argument("--archive-name", metavar="ARCHIVE_NAME", help=argparse.SUPPRESS)
    parser.add_argument("--volume-id", metavar="VOLUME_ID", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--restore-plan", metavar="PLAN", help=argparse.SUPPRESS)
    parser.add_argument("--restore-size", choices=["original", "min", "custom"], help=argparse.SUPPRESS)
    parser.add_argument("--new-vm-name", metavar="VM_NAME", help=argparse.SUPPRESS)
    parser.add_argument("--restore-verification", choices=["auto", "full"], default="auto", help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    args=parser.parse_args()
    c=load_config(args.config)
    # Never inherit destructive behavior from a reusable configuration file.
    c["delete_source"] = bool(args.delete_source)
    c["resize_mode"] = args.resize
    if args.dry_run:
        if args.command not in ("archive", "restore"):
            parser.error("--dry-run is available only with archive or restore")
        if args.delete_source:
            parser.error("--dry-run cannot be combined with --delete-source")
        if args.command == "archive" and args.resize == "min":
            parser.error("--dry-run cannot calculate a minimum ext4 size without booting the helper; use plan-resize instead")
        if args.command == "restore" and not args.restore_plan:
            parser.error("restore --dry-run requires an explicit --restore-plan")
    if args.linode_id and args.linode_label:
        parser.error("use either --linode-id or --linode-label, not both")
    if args.linode_id:
        c["source_linode_id"] = args.linode_id
    if args.linode_label:
        c["source_linode_label"] = args.linode_label
    if args.archive_name:
        validate_volume_label(args.archive_name)
        c["archive_label"] = args.archive_name
        if args.command in ("restore", "verify-archive"):
            c["archive_lookup_label"] = args.archive_name
    if args.volume_id: c["archive_volume_id"] = args.volume_id
    c["restore_plan_explicit"] = bool(args.restore_plan)
    c["restore_size"] = args.restore_size
    c["restore_verification"] = args.restore_verification
    if args.restore_plan: c["restore_plan"] = args.restore_plan
    if args.new_vm_name:
        validate_linode_label(args.new_vm_name)
        c["restore_label"] = args.new_vm_name
    if args.command in ("plan-resize", "recover-resize", "archive", "verify-archive", "prepare-bsv-helper"):
        if not c.get("source_linode_id") and not c.get("source_linode_label"):
            parser.error("archive commands require --linode-id or --linode-label")
        if args.command == "verify-archive" and not c.get("archive_lookup_label") and not c.get("archive_volume_id"):
            parser.error("verify-archive requires --archive-name or --volume-id")
    else:
        if not c.get("archive_lookup_label") and not c.get("archive_volume_id"):
            parser.error("restore commands require --archive-name or --volume-id")
    api=Linode(require_token())
    if c.get("source_linode_label"):
        source = linode_by_label(api, c["source_linode_label"])
        c["source_linode_id"] = source["id"]
        milestone(f"Selected source VM {source['label']} (Linode {source['id']}) by exact label")
    if args.command == "restore" and not args.dry_run and not c.get("restore_label"):
        c["restore_label"] = next_restore_label(api, load_archive_metadata(api, c).get("source_label"))
        milestone(f"No restored VM name supplied; using {c['restore_label']}")
    if args.command=="plan-resize": plan_resize(api,c)
    elif args.command=="recover-resize": recover_resize_source(api,c)
    elif args.command=="archive": archive_plan_bsv(api,c) if args.dry_run else (archive_bsv_resize_min(api,c) if c.get("resize_mode") == "min" else archive_bsv(api,c))
    elif args.command=="restore": restore_plan(api,c,load_archive_metadata(api,c)) if args.dry_run else restore_bsv(api,c)
    elif args.command=="verify-archive": verify_archive_bsv(api,c)
    elif args.command=="prepare-bsv-helper": prepare_bsv_helper(api,c)

READY_TAG = "cold-archive-helper-v1"
BUILDING_TAG = "cold-archive-helper-building-v1"
# B8's independently boot-tested helper uses the same worker protocol as v1.0.
# Keep this compatibility list explicit: a future release may reuse a prior
# helper only after its worker protocol has been deliberately verified.
COMPATIBLE_HELPER_READY_TAGS = (READY_TAG, "cold-archive-helper-b1")

# This runs only on the disposable, ordinary Debian builder.  It waits for the
# controller to attach the blank target BSV, then installs an independent OS
# directly onto that BSV.  Nothing is raw-copied from the builder's root disk.
BUILDER_STACKSCRIPT = r'''#!/bin/bash
# <UDF name="TARGET_VOLUME_LABEL" label="Target helper volume label" />
set -Eeuo pipefail
RESULT=/root/linode-cold-archive-helper-build.json
PROGRESS=/root/linode-cold-archive-helper-build-progress.json
progress() { printf '{"stage":"%s","updated_at":%s}\n' "$1" "$(date +%s)" > "$PROGRESS.tmp" && mv "$PROGRESS.tmp" "$PROGRESS"; }
fail() { printf '{"ok":false,"error":"%s"}\n' "$1" > "$RESULT"; exit 1; }
trap 'fail "builder failed at line $LINENO"' ERR
progress waiting_for_volume
TARGET_LINK="/dev/disk/by-id/scsi-0Linode_Volume_${TARGET_VOLUME_LABEL}"
for i in $(seq 1 180); do [ -e "$TARGET_LINK" ] && break; sleep 5; done
[ -e "$TARGET_LINK" ] || fail "timed out waiting for target Block Storage volume"
TARGET=$(readlink -f "$TARGET_LINK")
case "$TARGET" in /dev/sd[a-z]|/dev/vd[a-z]) ;; *) fail "unsafe target device $TARGET" ;; esac
progress installing_builder_tools
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -yq debootstrap grub-pc linux-image-amd64 openssh-server ca-certificates parted
progress partitioning_helper_volume
wipefs -a "$TARGET"
parted -s "$TARGET" mklabel msdos mkpart primary ext4 1MiB 100% set 1 boot on
# The virtual block device can take longer than a few seconds to expose its
# newly-created partition.  Request a table re-read, then wait explicitly.
partprobe "$TARGET" || true
partx -u "$TARGET" || true
PART="${TARGET}1"
for i in $(seq 1 30); do [ -b "$PART" ] && break; sleep 2; done
[ -b "$PART" ] || fail "target partition was not created"
mkfs.ext4 -F -L COLD_ARCHIVE_HELPER "$PART"
mkdir -p /mnt/cold-archive-helper
mount "$PART" /mnt/cold-archive-helper
progress installing_portable_debian
debootstrap --variant=minbase bookworm /mnt/cold-archive-helper http://deb.debian.org/debian
mount --bind /dev /mnt/cold-archive-helper/dev
mount -t proc proc /mnt/cold-archive-helper/proc
mount -t sysfs sys /mnt/cold-archive-helper/sys
UUID=$(blkid -s UUID -o value "$PART")
cat > /mnt/cold-archive-helper/etc/fstab <<EOF
UUID=$UUID / ext4 defaults 0 1
proc /proc proc defaults 0 0
EOF
cat > /mnt/cold-archive-helper/etc/apt/sources.list <<EOF
deb http://deb.debian.org/debian bookworm main
deb http://deb.debian.org/debian-security bookworm-security main
EOF
# A direct-disk boot can expose the public NIC as eth0, ens*, or enp*.
# systemd-networkd matches all of those names rather than baking in the
# builder's interface name/MAC as cloud-init or ifupdown would.
mkdir -p /mnt/cold-archive-helper/etc/systemd/network
cat > /mnt/cold-archive-helper/etc/systemd/network/10-cold-archive-dhcp.network <<EOF
[Match]
Name=eth* en*
[Network]
DHCP=yes
EOF
mkdir -p /mnt/cold-archive-helper/root/.ssh
chmod 700 /mnt/cold-archive-helper/root/.ssh
cat /root/.ssh/authorized_keys > /mnt/cold-archive-helper/root/.ssh/authorized_keys
chmod 600 /mnt/cold-archive-helper/root/.ssh/authorized_keys
cat > /mnt/cold-archive-helper/etc/hostname <<EOF
cold-archive-helper
EOF
progress installing_helper_runtime
chroot /mnt/cold-archive-helper /bin/bash -c 'apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -yq linux-image-amd64 grub-pc openssh-server ca-certificates'
chroot /mnt/cold-archive-helper /bin/bash -c 'systemctl enable systemd-networkd ssh'
cat > /mnt/cold-archive-helper/usr/local/sbin/linode-cold-archive-worker <<'WORKER'
#!/bin/bash
set -Eeuo pipefail
[ "$#" -eq 4 ] || { echo 'usage: worker MODE SOURCE DESTINATION COPY_BYTES' >&2; exit 64; }
MODE=$1; SOURCE=$2; DESTINATION=$3; COPY_BYTES=$4
RESULT=/root/linode-cold-archive-result.json; PROGRESS=/root/linode-cold-archive-progress.json
progress() { printf '{"phase":"%s","bytes":%s,"total_bytes":%s,"updated_at":%s}\n' "$1" "$2" "$3" "$(date +%s)" > "$PROGRESS.tmp" && mv "$PROGRESS.tmp" "$PROGRESS"; }
fail() { printf '{"ok":false,"error":"%s"}\n' "$1" > "$RESULT"; exit 1; }
for dev in "$SOURCE" "$DESTINATION"; do [ -b "$dev" ] || fail "$dev is not a block device"; done
src=$(blockdev --getsize64 "$SOURCE"); dst=$(blockdev --getsize64 "$DESTINATION")
[ "$COPY_BYTES" -gt 0 ] || fail 'copy length must be supplied by controller'
[ "$src" -ge "$COPY_BYTES" ] || fail 'source smaller than requested copy length'
[ "$dst" -ge "$COPY_BYTES" ] || fail 'destination smaller than requested copy length'
io() { awk -v key="$2" '$1 == key ":" {print $2}' "/proc/$1/io" 2>/dev/null || echo 0; }
watch() { while kill -0 "$1" 2>/dev/null; do progress "$2" "$(io "$1" "$3")" "$4"; sleep 10; done; }
if [ "$MODE" = verify ]; then
  progress source_checksum 0 "$COPY_BYTES"; head -c "$COPY_BYTES" "$SOURCE" | sha256sum >/tmp/source.hash & p=$!; watch $p source_checksum rchar "$COPY_BYTES" & w=$!; wait $p; kill $w 2>/dev/null || true
  progress destination_checksum 0 "$COPY_BYTES"; head -c "$COPY_BYTES" "$DESTINATION" | sha256sum >/tmp/destination.hash & p=$!; watch $p destination_checksum rchar "$COPY_BYTES" & w=$!; wait $p; kill $w 2>/dev/null || true
  a=$(awk '{print $1}' /tmp/source.hash); b=$(awk '{print $1}' /tmp/destination.hash); match=false; [ "$a" = "$b" ] && match=true
  printf '{"ok":true,"mode":"verify","source_sha256":"%s","destination_sha256":"%s","match":%s}\n' "$a" "$b" "$match" > "$RESULT"; exit 0
fi
progress copy 0 "$COPY_BYTES"; dd if="$SOURCE" of="$DESTINATION" bs=16M iflag=fullblock,count_bytes count="$COPY_BYTES" conv=fsync status=none & p=$!; watch $p copy write_bytes "$COPY_BYTES" & w=$!; wait $p; kill $w 2>/dev/null || true
sync; blockdev --flushbufs "$DESTINATION" 2>/dev/null || true
if [ "$MODE" = restore_auto ]; then
  progress destination_checksum 0 "$COPY_BYTES"; head -c "$COPY_BYTES" "$DESTINATION" | sha256sum >/tmp/destination.hash & p=$!; watch $p destination_checksum rchar "$COPY_BYTES" & w=$!; wait $p; kill $w 2>/dev/null || true
  b=$(awk '{print $1}' /tmp/destination.hash)
  printf '{"ok":true,"mode":"restore_auto","source_bytes":%s,"destination_sha256":"%s","sha256":"%s"}\n' "$COPY_BYTES" "$b" "$b" > "$RESULT"; exit 0
fi
progress source_checksum 0 "$COPY_BYTES"; head -c "$COPY_BYTES" "$SOURCE" | sha256sum >/tmp/source.hash & p=$!; watch $p source_checksum rchar "$COPY_BYTES" & w=$!; wait $p; kill $w 2>/dev/null || true
progress destination_checksum 0 "$COPY_BYTES"; head -c "$COPY_BYTES" "$DESTINATION" | sha256sum >/tmp/destination.hash & p=$!; watch $p destination_checksum rchar "$COPY_BYTES" & w=$!; wait $p; kill $w 2>/dev/null || true
a=$(awk '{print $1}' /tmp/source.hash); b=$(awk '{print $1}' /tmp/destination.hash); [ "$a" = "$b" ] || fail "checksum mismatch source=$a destination=$b"
printf '{"ok":true,"mode":"%s","source_bytes":%s,"source_sha256":"%s","destination_sha256":"%s","sha256":"%s"}\n' "$MODE" "$COPY_BYTES" "$a" "$b" "$a" > "$RESULT"
WORKER
chmod 700 /mnt/cold-archive-helper/usr/local/sbin/linode-cold-archive-worker
progress installing_bootloader
chroot /mnt/cold-archive-helper grub-install --target=i386-pc "$TARGET"
chroot /mnt/cold-archive-helper update-grub
sync
umount /mnt/cold-archive-helper/dev /mnt/cold-archive-helper/proc /mnt/cold-archive-helper/sys
umount /mnt/cold-archive-helper
printf '{"ok":true,"target":"%s"}\n' "$TARGET" > "$RESULT"
progress complete
'''

def ensure_helper_builder_stackscript(api, c):
    label = c.get("helper_builder_stackscript_label", "linode-cold-archive-built-helper-v1")
    stacks = api.get("/linode/stackscripts?page=1&page_size=100")["data"]
    matches = [s for s in stacks if s["label"] == label]
    if matches:
        current = max(matches, key=lambda s: s["id"])
        api.put(f"/linode/stackscripts/{current['id']}", {
            "script": BUILDER_STACKSCRIPT,
            "rev_note": "Build helper BSV directly; no raw template cloning",
        })
        return current["id"]
    return api.post("/linode/stackscripts", {
        "label": label,
        "description": "Builds the cold-archive helper BSV directly from Debian.",
        "images": [c["helper_image"]], "is_public": False, "script": BUILDER_STACKSCRIPT,
    })["id"]

def create_config(api, node_id, label, sda, sdb=None, sdc=None, sdd=None, kernel="linode/grub2", root_device=None):
    """Create a configuration that boots the partitioned helper BSV directly.

    The BSV contains its own MBR/GRUB and a single bootable root partition.
    ``linode/grub2`` is the provider boot helper and does not reliably locate
    that external-volume GRUB installation; direct-disk is the deliberate
    pattern used by the working Windows install-media StackScript.
    """
    devices = {"sda": sda, "sdb": sdb, "sdc": sdc, "sdd": sdd}
    root = root_device or "/dev/sda"
    if sda and sda.get("volume_id") and kernel == "linode/grub2":
        kernel, root = "linode/direct-disk", "/dev/sda1"
    return post_when_linode_ready(
        api, f"/linode/instances/{node_id}/configs",
        {"label": label, "kernel": kernel, "root_device": root, "devices": devices},
        "create the boot configuration",
    )

def ssh(c, ip, command, timeout=30):
    return subprocess.run(helper_ssh_command(c, ip, command), text=True,
                          capture_output=True, timeout=timeout)

def start_bsv_worker(c, ip, mode, copy_bytes, archive_volume_label):
    """Start the worker after resolving BSVs by-id and local disks by exclusion."""
    resolver = r'''set -Eeuo pipefail
RESULT=/root/linode-cold-archive-result.json
fail() { printf '{"ok":false,"error":"%s"}\n' "$1" > "$RESULT"; exit 1; }
trap 'fail "worker launcher failed at line $LINENO"' ERR
disk_name() {
  local device="$1" parent
  parent=$(lsblk -dno PKNAME "$device" 2>/dev/null || true)
  [ -n "$parent" ] && { echo "$parent"; return; }
  basename "$device" | sed 's/[0-9]*$//'
}
root_source=$(findmnt -n -o SOURCE /)
root_disk=$(disk_name "$root_source")
archive_link="/dev/disk/by-id/scsi-0Linode_Volume_$3"
[ -e "$archive_link" ] || fail "expected archive volume by-id link is absent: $archive_link"
archive_disk=$(disk_name "$(readlink -f "$archive_link")")
volume_disks=()
for link in /dev/disk/by-id/scsi-0Linode_Volume_*; do
  [ -e "$link" ] || continue
  case "$link" in *-part*) continue;; esac
  volume_disks+=("$(disk_name "$(readlink -f "$link")")")
done
is_volume_disk() { local candidate="$1" disk; for disk in "${volume_disks[@]}"; do [ "$candidate" = "$disk" ] && return 0; done; return 1; }
copy_bytes=$2
eligible=()
while read -r local_disk; do
  [ -n "$local_disk" ] || continue
  [ "$local_disk" != "$root_disk" ] || continue
  is_volume_disk "$local_disk" && continue
  bytes=$(blockdev --getsize64 "/dev/$local_disk")
  if [ "$1" = archive ] || [ "$bytes" -ge "$copy_bytes" ]; then
    eligible+=("$bytes:$local_disk")
  fi
done < <(lsblk -dn -o NAME,TYPE | awk '$2 == "disk" {print $1}')
[ "${#eligible[@]}" -gt 0 ] || fail "no eligible non-BSV local disk is available for the copy"
IFS=$'\n' eligible=($(sort -nr <<<"${eligible[*]}")); unset IFS
if [ "$1" != archive ] && [ "${#eligible[@]}" -ne 1 ]; then
  fail "local target disk is ambiguous after excluding helper/archive BSVs"
fi
local_disk=${eligible[0]#*:}
if [ "$1" = archive ]; then
  source="/dev/$local_disk"; destination="/dev/$archive_disk"
  [ "$copy_bytes" -eq 0 ] && copy_bytes=$(blockdev --getsize64 "$source")
  if [ "$2" -ne 0 ] && [ "$(blockdev --getsize64 "$source")" -ne "$copy_bytes" ]; then
    fail "resolved source local disk does not match the qualified compact source size"
  fi
else
  source="/dev/$archive_disk"; destination="/dev/$local_disk"
fi
exec /usr/local/sbin/linode-cold-archive-worker "$1" "$source" "$destination" "$copy_bytes"'''
    remote = (
        "rm -f /root/linode-cold-archive-result.json /root/linode-cold-archive-progress.json /root/linode-cold-archive-worker.log; "
        f"nohup /bin/bash -c {shlex.quote(resolver)} -- {shlex.quote(mode)} {int(copy_bytes)} {shlex.quote(archive_volume_label)} "
        ">/root/linode-cold-archive-worker.log 2>&1 &"
    )
    end, result = time.time() + 600, None
    while time.time() < end:
        try:
            result = ssh(c, ip, remote)
            if result.returncode == 0:
                return
        except subprocess.SubprocessError:
            pass
        time.sleep(5)
    detail = "" if result is None else (result.stderr.strip() or result.stdout.strip())
    raise RuntimeError("Unable to start the helper worker over SSH: " + detail)

def helper_result(c, ip, workflow="copy"):
    """Poll worker state without hiding malformed worker output as an SSH wait."""
    remote = (
        "if [ -s /root/linode-cold-archive-result.json ]; then "
        "cat /root/linode-cold-archive-result.json; "
        "elif [ -s /root/linode-cold-archive-progress.json ]; then "
        "cat /root/linode-cold-archive-progress.json; "
        "else echo '{\"state\":\"waiting\"}'; fi"
    )
    labels = {
        "copy": "Step 1/4: raw-copy source device to destination device",
        "source_checksum": "Step 2/4: calculate SHA-256 of copy source",
        "destination_checksum": "Step 3/4: calculate SHA-256 of copy destination",
    }
    if workflow == "verify":
        labels = {
            "source_checksum": "Step 1/2: calculate SHA-256 of original root disk",
            "destination_checksum": "Step 2/2: calculate SHA-256 of archive volume",
        }
    elif workflow == "restore_auto":
        labels = {
            "copy": "Step 1/2: raw-copy archive BSV to restored local disk",
            "destination_checksum": "Step 2/2: calculate SHA-256 of restored local disk against the verified archive hash tag",
        }
    elif workflow == "restore":
        labels = {
            "copy": "Step 1/3: raw-copy archive BSV to restored local disk",
            "source_checksum": "Step 2/3: calculate SHA-256 of archive BSV",
            "destination_checksum": "Step 3/3: calculate SHA-256 of restored local disk",
        }
    end, next_report, last, last_phase = time.time() + c.get("copy_timeout_seconds", 86400), 0, None, None
    next_wait_diagnostic = time.time() + 30
    milestone("Waiting for the helper copy and checksum...")
    while time.time() < end:
        result = ssh(c, ip, remote)
        if result.returncode:
            if time.time() >= next_report:
                milestone("Waiting for helper SSH/progress endpoint...")
                next_report = time.time() + 60
            time.sleep(5); continue
        raw = result.stdout.strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Helper returned malformed JSON: {raw!r}") from exc
        if "ok" in data:
            if not data["ok"]:
                raise RuntimeError(f"Helper failed: {data}")
            return data
        if data.get("state") == "waiting" and time.time() >= next_wait_diagnostic:
            diagnostic = ssh(c, ip, "if pgrep -f '^/usr/local/sbin/linode-cold-archive-worker' >/dev/null; then echo running; else echo stopped; fi; tail -n 12 /root/linode-cold-archive-worker.log 2>/dev/null || true")
            lines = diagnostic.stdout.strip().splitlines() if diagnostic.returncode == 0 else []
            state = lines[0] if lines else "unknown"
            log = "\n".join(lines[1:]).strip()
            if state == "stopped" and log:
                raise RuntimeError("Helper worker exited before reporting progress: " + log)
            detail = f" Worker state: {state}." if state else ""
            milestone("Worker has not reported progress yet; checking helper status." + detail)
            next_wait_diagnostic = time.time() + 60
        if data.get("phase") and time.time() >= next_report:
            done, total = data.get("bytes", 0), data.get("total_bytes", 0)
            if data["phase"] != last_phase:
                milestone(labels.get(data["phase"], f"Helper phase: {data['phase']}"))
                last_phase, last = data["phase"], None
            speed = ""
            if last and done >= last[0]:
                elapsed = time.time() - last[1]
                if elapsed > 0: speed = f", {(done-last[0])/elapsed/1_000_000:.1f} MB/s"
            pct = f"{100 * done / total:.1f}%" if total else "?%"
            milestone(f"Helper {data['phase']}: {pct} ({done/1_000_000_000:.1f}/{total/1_000_000_000:.1f} GB{speed})")
            last, next_report = (done, time.time()), time.time() + 15
        time.sleep(5)
    raise TimeoutError("Timed out waiting for the helper copy and checksum")

def wait_ssh(c, ip, name, seconds=900):
    def ready():
        return ssh(c, ip, "true").returncode == 0
    return wait_until(ready, f"{name} to become SSH-ready", seconds=seconds)

def helper_build_status(c, ip):
    result = ssh(c, ip, "cat /root/linode-cold-archive-helper-build-progress.json 2>/dev/null; cat /root/linode-cold-archive-helper-build.json 2>/dev/null")
    return result.stdout.strip()

def wait_builder(c, ip):
    milestone("Stage 3.1/5: builder is installing Debian directly onto the helper BSV")
    end, seen = time.time() + 1800, None
    while time.time() < end:
        result = ssh(c, ip, "cat /root/linode-cold-archive-helper-build.json 2>/dev/null || true")
        if result.stdout.strip():
            data = json.loads(result.stdout)
            if not data.get("ok"):
                raise RuntimeError("Helper BSV builder failed: " + json.dumps(data))
            return
        progress = helper_build_status(c, ip)
        if progress and progress != seen:
            seen = progress
            try:
                stage = json.loads(progress.splitlines()[0]).get("stage")
                if stage: milestone(f"Stage 3.2/5: helper BSV builder: {stage.replace('_', ' ')}")
            except json.JSONDecodeError:
                pass
        time.sleep(10)
    raise TimeoutError("Timed out waiting for the helper BSV builder")

def wait_for_helper_worker(c, ip, timeout_seconds=120):
    """Wait for the portable helper's installed worker to become visible.

    SSH availability is a useful boot signal but does not guarantee every
    userspace path is ready at that exact instant.  Keep the independent probe
    test, but retry its very small executable check for a bounded period.
    """
    end = time.time() + timeout_seconds
    announced_wait = False
    last_detail = ""
    while time.time() < end:
        test = ssh(c, ip, "test -x /usr/local/sbin/linode-cold-archive-worker")
        if test.returncode == 0:
            return
        last_detail = (test.stderr or test.stdout).strip()
        if not announced_wait:
            milestone("Stage 4.1/5: helper runtime is still settling; waiting for its copy worker")
            announced_wait = True
        time.sleep(5)
    suffix = f" Details: {last_detail}" if last_detail else ""
    raise RuntimeError("Helper BSV booted, but its copy worker did not become executable within 120 seconds." + suffix)

def helper_boot_volume(api, c, region):
    volumes = api.get("/volumes?page=1&page_size=100")["data"]
    existing = [
        v for v in volumes
        if v.get("region") == region and any(tag in COMPATIBLE_HELPER_READY_TAGS for tag in v.get("tags", []))
    ]
    if len(existing) > 1:
        raise RuntimeError(f"Multiple compatible cold-archive helper BSVs exist in {region}: " + ", ".join(str(v["id"]) for v in existing) + ". Keep one before retrying.")
    if existing:
        matched = next(tag for tag in existing[0].get("tags", []) if tag in COMPATIBLE_HELPER_READY_TAGS)
        if matched != READY_TAG:
            milestone(f"Reusing compatible existing helper BSV {existing[0]['id']} ({matched}); it will not be retagged")
        return existing[0]
    # Linode volume labels are limited to 32 characters. This concise form
    # remains deterministic and fits even a long region ID such as
    # ``us-southeast`` (exactly 32 characters).
    helper_label = derived_volume_label("cold-archive-helper", f"-{region}")
    label_conflicts = [v for v in volumes if v.get("region") == region and v.get("label") == helper_label]
    if label_conflicts:
        conflict = label_conflicts[0]
        raise RuntimeError(
            f"A helper BSV with label {helper_label!r} already exists in {region}: volume {conflict['id']} "
            f"with tags {conflict.get('tags', [])}. It is missing a recognized ready tag "
            f"({', '.join(COMPATIBLE_HELPER_READY_TAGS)}). Confirm that it is a boot-tested compatible helper "
            "before adding a recognized tag manually; otherwise delete it, then retry. "
            "Version 1.0 will not retag, replace, or reuse an unknown helper BSV."
        )
    size = int(c.get("helper_boot_volume_gb", 10))
    print(
        f"This will create a reusable {size} GB helper Block Storage volume in {region}.\n"
        "It will also briefly create disposable builder and probe Linodes to install and boot-test it.",
        flush=True,
    )
    require_yes(f"CREATE BSV HELPER IN {region}")
    milestone(f"Stage 1/5: creating reusable {size} GB Debian helper BSV in {region}")
    volume = api.post("/volumes", {"label": helper_label, "region": region, "size": size,
                                   "tags": [BUILDING_TAG, f"ca-helper-region-{region}"]})
    builder_id = probe_id = None
    completed = False
    try:
        stack = ensure_helper_builder_stackscript(api, c)
        types = helper_builder_candidates(api, region, 2048)
        if not types: raise RuntimeError("No g6/g7/g8 temporary builder plan is available in this region.")
        builder_type = types[0]["id"]
        milestone(f"Stage 2/5: creating disposable {builder_type} Debian builder")
        started = datetime.now(timezone.utc)
        builder = api.post("/linode/instances", {"region": region, "type": builder_type, "label": f"cold-archive-helper-builder-{int(time.time())}"})
        builder_id = builder["id"]
        builder = wait_for_linode_create(api, builder_id, started, "helper builder Linode")
        disk = post_when_linode_ready(api, f"/linode/instances/{builder_id}/disks", {
            "label": "cold-archive-helper-builder", "size": 4096, "image": c["helper_image"],
            "authorized_keys": [c["helper_ssh_public_key"]], "stackscript_id": stack,
            "stackscript_data": {"TARGET_VOLUME_LABEL": volume["label"]},
        }, "create the helper builder disk")
        config = create_config(api, builder_id, "cold-archive-helper-builder", {"disk_id": disk["id"]})
        milestone("Stage 2.1/5: booting the Debian builder and waiting for its SSH endpoint")
        boot(api, builder_id, config["id"], "Debian helper builder")
        builder = api.get(f"/linode/instances/{builder_id}")
        ip = builder["ipv4"][0]
        wait_ssh(c, ip, "Debian helper builder")
        milestone("Stage 3/5: attaching the blank helper BSV to the running builder")
        api.post(f"/volumes/{volume['id']}/attach", {"linode_id": builder_id, "persist_across_boots": False})
        wait_builder(c, ip)
        milestone("Stage 3.3/5: direct helper BSV build completed")
        shutdown(api, builder_id)
        wait_until(lambda: api.get(f"/volumes/{volume['id']}").get("linode_id") is None,
                      "helper BSV to detach from the builder")
        milestone("Stage 4/5: boot-testing the completed helper BSV on a separate probe")
        probe_started = datetime.now(timezone.utc)
        probe = api.post("/linode/instances", {"region": region, "type": builder_type, "label": f"cold-archive-helper-probe-{int(time.time())}"})
        probe_id = probe["id"]
        probe = wait_for_linode_create(api, probe_id, probe_started, "helper probe Linode")
        probe_cfg = create_config(api, probe_id, "cold-archive-helper-probe", {"volume_id": volume["id"]}, kernel="linode/grub2")
        boot(api, probe_id, probe_cfg["id"], "helper BSV probe")
        probe = api.get(f"/linode/instances/{probe['id']}")
        wait_ssh(c, probe["ipv4"][0], "portable helper BSV")
        wait_for_helper_worker(c, probe["ipv4"][0])
        milestone("Stage 4.1/5: independent helper BSV boot and worker self-test passed")
        shutdown(api, probe_id)
        # A volume referenced by the probe configuration remains attached after
        # shutdown until explicitly detached.  Do this before waiting for the
        # provider to report a detached helper BSV.
        api.post(f"/volumes/{volume['id']}/detach", {})
        wait_until(lambda: api.get(f"/volumes/{volume['id']}").get("linode_id") is None,
                      "helper BSV to detach from the probe")
        delete_when_linode_ready(api, f"/linode/instances/{probe_id}", "remove the helper probe")
        probe_id = None
        api.put(f"/volumes/{volume['id']}", {"tags": [READY_TAG, f"ca-helper-region-{region}"]})
        milestone(f"Stage 5/5: helper BSV {volume['id']} is ready in {region}")
        completed = True
        return api.get(f"/volumes/{volume['id']}")
    finally:
        # A failed build must not leave billable builder/probe Linodes behind.
        # Keep the BSV tagged "building" for diagnosis, never as a reusable helper.
        for node_id, role in ((probe_id, "helper probe"), (builder_id, "Debian helper builder")):
            if not node_id:
                continue
            try:
                shutdown(api, node_id)
                delete_when_linode_ready(api, f"/linode/instances/{node_id}", f"remove the disposable {role}")
            except Exception as exc:
                milestone(f"WARNING: disposable {role} {node_id} still needs manual deletion: {exc}")
        if not completed:
            try:
                api.post(f"/volumes/{volume['id']}/detach", {})
            except Exception:
                pass
            milestone(f"Helper BSV build did not complete. Volume {volume['id']} remains tagged {BUILDING_TAG} for diagnosis; delete it when no longer needed.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        if ACTIVE_CLEANUPS:
            milestone("Workflow interrupted; cleaning up temporary BSV helper attachments and configuration")
            run_active_cleanups()
        print("ERROR: Cancelled by operator.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        if ACTIVE_CLEANUPS:
            milestone("Workflow failed; cleaning up temporary BSV helper attachments and configuration")
            run_active_cleanups()
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
