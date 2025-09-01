# bbbb

Btrfs Bootable Backup Bimage is a script that creates incremental backups of a running Linux system in the form of bootable disk image.
It does so with the magic of `dd` and btrfs snapshots. It is intended to have a reasonably low downtime on systems where you cannot or
don't want to have a RAID setup, like single board computers used as servers.

**WARNING!! This is in a very early development stage and will most likely destroy all your data!**

## Requirements

### Server

This is where the script runs and the disk images are stored.

- Root access
- Passwordless SSH access to the client as root
- `btrbk`
- `losetup`
- Python 3

### Client

- Root access
- Only two partitions: A boot or EFI partition first, then btrfs. 
