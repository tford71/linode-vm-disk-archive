# Linode VM Disk Archive 2.0 — Quick Start

Use `linode_vm_disk_archive.py` to preserve a Linode VM's root local disk in a
verified Block Storage volume (BSV), then create a new VM from that archive.

The BSV's tags contain the recovery metadata. Any JSON archive log written
locally is an audit record only.

## 1. Install the release files

The controller can be Linux, macOS, Windows with a suitable Python/SSH
environment, WSL, or a management VM. It needs Python 3, an OpenSSH `ssh`
client, outbound HTTPS access to `api.linode.com`, and TCP/22 access to the VM
while it boots the temporary helper environment.

There are no Python packages to install. Copy the release files to the
controller, then create a local configuration file:

```bash
mkdir -p ~/linode-vm-disk-archive
# Copy release files into ~/linode-vm-disk-archive by your normal secure method.
cd ~/linode-vm-disk-archive
cp config.example.json config.json
```

## 2. Create credentials

Create a dedicated automation SSH keypair. It is used only to reach temporary
Debian helper boots; it is not the guest OS administrator key.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/linode-vm-disk-archive-automation \
  -C linode-vm-disk-archive-automation
chmod 600 ~/.ssh/linode-vm-disk-archive-automation
```

Edit `config.json` and provide the complete public key plus the actual private
key path used by your controller:

```json
{
  "archive_directory": "./archives",
  "helper_image": "linode/debian13",
  "helper_ssh_public_key": "paste the contents of the .pub file here",
  "helper_ssh_private_key": "/home/your-user/.ssh/linode-vm-disk-archive-automation",
  "copy_timeout_seconds": 86400
}
```

Create a Linode personal access token with sufficient permissions for Linodes,
disks/configurations, Block Storage volumes, StackScripts, events, and type/
region lookups. Keep the token out of `config.json` and export it for each
controller session:

```bash
export LINODE_TOKEN='your-token-here'
python3 linode_vm_disk_archive.py --version
```

Before archive or restore, make sure the relevant firewall/network path permits
TCP/22 from the controller. A restrictive source firewall still applies while
the source is booted into the temporary helper.

## 3. Archive a VM

The simplest archive command uses the source VM's exact label:

```bash
python3 linode_vm_disk_archive.py archive --linode-label my-vm
```

For a supported whole-device Linux `ext4` root, this measures the filesystem,
adds a 4 GB safety buffer, and creates the smallest supported archive BSV.
After verification, the source root disk and filesystem return to their
original size and the source remains powered off.

Compact source disks and archive BSVs are rounded up to whole GB, with a 10 GB
minimum. A small raw-device cushion is added when restoring to a newly created
local disk.

If you explicitly want a full-size archive, add `--resize original`:

```bash
python3 linode_vm_disk_archive.py archive --linode-label my-vm --resize original
```
If the root cannot be safely shrunk, such as Windows or a partitioned root, the
tool explains why, creates no archive, and leaves the source unchanged. Rerun
with `--resize original` for a normal full-size archive.

The first archive or restore in a region automatically creates and boot-tests a
reusable 10 GB helper BSV. Running `prepare-bsv-helper` separately is optional.

To delete a source only after a verified archive, use the explicit flag. The
tool requires a separate final exact confirmation before deletion:

```bash
python3 linode_vm_disk_archive.py archive \
  --linode-label my-vm \
  --delete-source
```

## 4. Restore a new VM

Restore by the archive BSV's current label:

```bash
python3 linode_vm_disk_archive.py restore --archive-name my-vm-archive
```

The archived source plan is offered first. If no VM name is supplied, the new
VM receives the first unused name such as `my-vm-r1`.

For a compact archive, the default is to restore the original root-disk
allocation. You can retain the compact allocation or choose a custom root size:

```bash
python3 linode_vm_disk_archive.py restore \
  --archive-name my-vm-archive \
  --restore-size min
```

If the archive BSV was renamed in Cloud Manager, use its current name. If the
name is ambiguous, restore using its immutable volume ID instead:

```bash
python3 linode_vm_disk_archive.py restore --volume-id 12345678
```

## 5. Confirm the restored VM

Confirm the guest OS boots, reapply Cloud Firewalls as needed, and verify new
public/VPC/VLAN interfaces, IP addresses, MAC addresses, and any guest
networking that relies on them.

Use a full two-sided checksum when desired:

```bash
python3 linode_vm_disk_archive.py restore \
  --archive-name my-vm-archive \
  --restore-verification full
```

## Useful checks

```bash
# Show commands and options.
python3 linode_vm_disk_archive.py --help

# Preview an ordinary archive without changing the source VM.
python3 linode_vm_disk_archive.py archive --linode-label my-vm --dry-run

# Validate an archive against a specific plan without creating a VM.
python3 linode_vm_disk_archive.py restore \
  --archive-name my-vm-archive \
  --restore-plan g6-standard-4 \
  --dry-run
```

Type `CANCEL` at any exact-confirmation prompt to abort safely.
