#!/usr/bin/env python
# -*- coding: utf-8 -*-

import subprocess
import paramiko
import sys
import argparse

def parse_parted_output(output):
	"""Parse the output of parted --machine and return disk size and partition info."""
	lines = output.strip().split('\n')
	if len(lines) < 3:
		raise ValueError("Invalid parted output")
	disk_info = lines[1].split(':')
	disk_size = disk_info[1].rstrip('B')
	partitions = []
	for line in lines[2:]:
		fields = line.split(':')
		if len(fields) < 5:
			continue
		part_num = fields[0]
		start = fields[1].rstrip('B')
		end = fields[2].rstrip('B')
		size = str(int(end) - int(start))
		fs_type = fields[4]
		partitions.append((part_num, size, start, fs_type))
	return disk_size, partitions

def get_partitions_ssh(ssh, device):
	"""Get partition information using parted. Returns disk size and for each partition: size, offset, filesystem type."""
	stdin, stdout, stderr = ssh.exec_command("parted --machine {} unit B print".format(device))
	return parse_parted_output(stdout.read().decode())

def get_partitions_local(device):
	"""Get partition information using parted locally. Returns disk size and for each partition: size, offset, filesystem type."""
	result = subprocess.run(["parted", "--machine", device, "unit", "B", "print"], capture_output=True, text=True)
	if result.returncode != 0:
		raise RuntimeError("Failed to run parted: {}".format(result.stderr))
	return parse_parted_output(result.stdout)

def get_partition_device(device, part_num):
	"""Get the partition device name based on the main device and partition number."""
	if device.startswith('/dev/mmcblk') or device.startswith('/dev/nvme'):
		return "{}p{}".format(device, part_num)
	else:
		return "{}{}".format(device, part_num)

def get_btrfs_partition_offset(partitions):
	# Find the first btrfs partition
	btrfs_partition = None
	for part in partitions:
		if part[3] == 'btrfs':
			btrfs_partition = part
			break
	if not btrfs_partition:
		raise ValueError("No btrfs partition found on the device.")
	return int(btrfs_partition[2])

def create_initial_image(ssh, device, output_file):
	"""
	Create a file in the same size as the device. Remount the first remote partition as read-only. 
	Copy the disk up to the start of the btrfs partition. Mount the image file as a loop device and
	create an empty btrfs filesystem on the second partition.
	"""
	disk_size, partitions = get_partitions_ssh(ssh, device)
	if not partitions:
		raise ValueError("No partitions found on the device.")

	# Get blkid of remote partition
	stdin, stdout, stderr = ssh.exec_command("blkid -o value -s UUID {}".format(get_partition_device(device, 2)))
	uuid = stdout.read().decode().strip()
	if not uuid:
		raise ValueError("Failed to get UUID of the btrfs partition.")
	
	# Remount the first partition as read-only
	first_partition = get_partition_device(device, 1)
	ssh.exec_command("mount -o remount,ro {}".format(first_partition))
	
	# Create an empty file of the same size as the disk
	with open(output_file, 'wb') as f:
		f.truncate(int(disk_size))
	
	# Read data up to the start of the btrfs partition and write it to the output file
	btrfs_start = get_btrfs_partition_offset(partitions)
	copy_command = "dd if={} bs=1K count={}".format(device, btrfs_start // 1024)
	stdin, stdout, stderr = ssh.exec_command(copy_command)
	with open(output_file, 'r+b') as f:
		while True:
			data = stdout.channel.recv(1024)
			if not data:
				break
			f.write(data)
			f.flush()

	# Create an empty btrfs filesystem in the output file at the correct offset
	loop_device = "/dev/loop0"
	mount_command = f"losetup {loop_device} --partscan {output_file}"
	subprocess.run(mount_command, shell=True, check=True)
	subprocess.run(f"mkfs.btrfs --uuid {uuid} {loop_device}p2", shell=True, check=True)
	subprocess.run("losetup --detach {}".format(loop_device), shell=True, check=True)

def run_backup(ssh, device, output_file, subvolume="@"):
	"""
	Do the actual backup with btrbk. Assumes the initial image has been created.
	"""
	mount_point = "/mnt"
	config_file = "/tmp/btrbk.conf"
	disk_size, partitions = get_partitions_local(output_file)
	# Loop-mount the second partition of the output file
	loop_device = "/dev/loop0"
	loop_command = f"losetup {loop_device} --partscan {output_file}"
	subprocess.run(loop_command, shell=True, check=True)
	local_mount_command = f"mount {loop_device}p2 -o subvolid=5 {mount_point}"
	subprocess.run(local_mount_command, shell=True, check=True)
	remote_mount_command = f"mount {get_partition_device(device, 2)} -o subvolid=5 {mount_point}"
	stdin, stdout, stderr = ssh.exec_command(remote_mount_command)
	print(stderr.read().decode())
	
	btrbk_config = f"""
volume {ssh.get_transport().getpeername()[0]}:/mnt
  target {mount_point}
  subvolume {subvolume}
"""
	# Write the btrbk config to a temporary file
	with open(config_file, 'w') as f:
		f.write(btrbk_config)
	# Run btrbk
	try:
		subprocess.run(["btrbk", "--config", config_file, "--format=raw", "run"], check=True)

		# Remove old rw subvolume
		subprocess.run(f"btrfs subvolume delete {mount_point}/{subvolume}", shell=True)

		# Find newest snapshot. Snapshots are named like @.20250831T0152
		result = subprocess.run(f"btrfs subvolume list {mount_point}", shell=True, capture_output=True, text=True)
		if result.returncode != 0:
			raise RuntimeError("Failed to list btrfs subvolumes: {}".format(result.stderr))
		lines = result.stdout.strip().split('\n')
		newest_snapshot = None
		for line in lines:
			fields = line.split()
			if len(fields) < 9:
				continue
			name = fields[8]
			# Skip names that do not start with "{subvolume}."
			if not name.startswith(f"{subvolume}."):
				continue
			if not newest_snapshot or name > newest_snapshot:
				newest_snapshot = name
		if not newest_snapshot:
			raise ValueError("No snapshots found.")

		# Create a new rw subvolume based on the newest snapshot
		subprocess.run(f"btrfs subvolume snapshot {mount_point}/{newest_snapshot} {mount_point}/{subvolume}", shell=True, check=True)
		subprocess.run(f"btrfs subvolume set-default {mount_point}/{subvolume}", shell=True, check=True)
	finally:
		# Cleanup
		subprocess.run(f"umount {mount_point}", shell=True, check=True)
		subprocess.run(f"losetup --detach {loop_device}", shell=True, check=True)
		ssh.exec_command(f"umount {mount_point}")

def main():
	parser = argparse.ArgumentParser(description="Backup partitions from a remote device over SSH.")
	parser.add_argument("hostname", help="Hostname or IP address of the remote device")
	parser.add_argument("device", help="Block device to back up (e.g., /dev/sda)")
	parser.add_argument("output", help="Output file for the disk image")
	args = parser.parse_args()

	# Establish SSH connection
	ssh = paramiko.SSHClient()
	ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
	try:
		ssh.connect(args.hostname, username="root")
	except paramiko.SSHException as e:
		print(f"SSH connection failed: {e}")
		sys.exit(1)

	# Check if output file exists. If not, create it. If it exists but has the wrong size, abort.
	stat_command = f"stat -c %s {args.output}"
	try:
		result = subprocess.run(stat_command, shell=True, capture_output=True, text=True)
		if result.returncode != 0:
			# File does not exist, create initial image
			print("Creating initial image...")
			create_initial_image(ssh, args.device, args.output)
		else:
			# File exists, check size
			local_size = int(result.stdout.strip())
			remote_size, _ = get_partitions_ssh(ssh, args.device)
			if local_size != int(remote_size):
				print(f"Output file size ({local_size}) does not match remote device size ({remote_size}). Aborting.")
				sys.exit(1)
	except Exception as e:
		print(f"Error checking or creating output file: {e}")
		sys.exit(1)

	# Run backup
	run_backup(ssh, args.device, args.output)
	ssh.close()

if __name__ == "__main__":
    main()

