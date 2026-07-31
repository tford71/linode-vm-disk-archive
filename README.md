# Linode VM Disk Archive 2.0

`linode_vm_disk_archive.py` archives a Linode VM's selected local boot disk to
a verified Block Storage volume (BSV), then restores it to a new Linode. It
uses a small reusable Debian helper BSV to perform raw copies, checksums, and,
when requested, supported Linux ext4 filesystem resizing.

The controller runs on any machine with Python 3 and an OpenSSH client: Linux,
macOS, Windows with a suitable Python/SSH environment, a management VM, or
WSL. It is not WSL-specific.

## What it preserves

- The selected root local disk is raw-copied and SHA-256 verified.
- One separate local swap disk is recorded as metadata and recreated blank on
  restore with its original UUID. Swap contents are not copied.
- Archive tags are the recovery source of truth. The optional local archive
  log is informational only and is never required to restore.

## What it does not preserve

- Attached Block Storage volumes are listed before archive confirmation, but
  are not copied.
- Additional local data disks are not copied.
- A restored VM is new infrastructure: it receives new IP addresses, MAC
  addresses, and network interfaces. Confirm guest networking and reapply
  Cloud Firewalls as needed after restore.

## Archive modes

By default, archive copies the full root local disk.

`--resize min` is an opt-in compact archive mode for a proven whole-device
Linux `ext4` root filesystem. It powers off the source, determines the minimum
filesystem size, adds a 4 GB safety buffer, temporarily shrinks the source
filesystem and local disk, and archives the smaller disk. After a successful
archive, the source is restored to its original disk allocation and filesystem
size unless `--delete-source` was explicitly requested.

Windows, partitioned roots, and unsupported filesystems are never shrunk. The
tool explains why and offers to continue with an ordinary full-size archive.

## Requirements

1. Python 3 and the `ssh` command on the controller machine.
2. A Linode personal access token with access to Linodes, disks,
   configurations, Block Storage volumes, StackScripts, events, and account
   type/region lookups. Export it in the controller environment:

   ```bash
   export LINODE_TOKEN='...'
   ```

3. A dedicated automation SSH keypair. Put the public key and controller
   private-key path in `config.json`; start from `config.example.json`.
4. Outbound HTTPS access from the controller to `api.linode.com`, plus TCP/22
   from the controller to each VM while that VM is temporarily booted into the
   archive helper. A restrictive source firewall remains in effect during the
   helper boot, so it must permit the controller path.

There are no Python packages to install; the program uses the standard
library and the system OpenSSH client.

## Typical workflow

```bash
# Archive a VM. The first archive or restore in a region automatically creates
# and independently boot-tests the reusable 10 GB helper BSV.
python3 linode_vm_disk_archive.py archive --linode-label my-vm --resize min

# Restore with the archived source plan and, for a compact archive, the
# original root allocation. The default name is my-vm-r1, then my-vm-r2, etc.
python3 linode_vm_disk_archive.py restore --archive-name my-vm-archive
```

Use `--linode-id` instead of `--linode-label` when you prefer an immutable
source identifier. Use `--volume-id` instead of `--archive-name` when a volume
has been renamed or its label is ambiguous.

`archive --dry-run` prints ordinary archive sizing without changing the source
or creating a BSV. `restore --dry-run --restore-plan PLAN` validates an archive
and a specific target plan without creating a VM. `plan-resize` remains an
advanced command because it temporarily boots the helper to inspect an offline
filesystem, but makes no filesystem changes.

## Verification and safety

Archive always raw-copies the source, hashes the source, hashes the archive
BSV, and requires matching SHA-256 values. Restore defaults to `auto`: it uses
the archive's verified checksum tag and hashes the new local disk. Use
`--restore-verification full` to hash both the archive BSV and restored disk.

Creating resources, compacting a filesystem, and deleting a source require
exact confirmation text. A mistyped response or Enter re-prompts; type
`CANCEL` or press Ctrl+C to abort.

`--delete-source` is a command-line-only choice. It is never read from
`config.json`, and a second exact confirmation is required after archive
verification. With `--resize min`, any failure before the verified archive is
complete triggers automatic source disk/filesystem recovery.

## Help

```bash
python3 linode_vm_disk_archive.py --help
python3 linode_vm_disk_archive.py --version
```

See [QUICKSTART.md](QUICKSTART.md) for setup and first-run instructions.
