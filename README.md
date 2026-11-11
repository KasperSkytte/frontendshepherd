# frontendshepherd

`frontendshepherd` is a tool to automatically limit the resources (CPU and memory) available to each user on the shared frontend (aka access/login) nodes of a computing cluster to keep them responsive. It prevents situations where one or a few users block access for everyone else by running large computing jobs directly on a frontend node, accidentally or deliberately, instead of submitting them as batch jobs to the dedicated compute nodes. Such jobs can use all CPU cores or, even worse, crash the node due to memory starvation. Users who exceed the limits are restricted automatically and receive an email explaining why.

**Memory usage** is hard-limited per user (by default 16GB), where all of the user's processes on the node are killed if the limit is exceeded. The total amount of memory available to all users together is also limited (by default 8GB is always kept for the host), so the node itself can never run out. (in the future `/tmp` will also be managed)

**CPU usage** is limited both with a max number of available CPUs per user, but also by using a "bucket" of CPU hours per user (by default 8 CPU hours), so that some burst load is always possible, for example for compiling and installing software, building containers, doing quick data analyses etc. When the bucket is empty, the user's processes are throttled to a bare minimum (by default 2 CPUs), which is still enough for longer `rsync` data transfers and for normal work like coding on the frontend node. The throttle is lifted when the bucket has refilled completely (by default after 4 hours), and it only refills while the user's average CPU usage is below the bare minimum.

**Stale processes** are rounded up by the "sheepdog": processes matching a pattern (like `vscode-server` processes!) are automatically killed after running for a max walltime (by default 7 days) to prevent unnecessary processes from accumulating over time.

`frontendshepherd` only restricts users. No resources are reserved for individual users, they are only limited, so all users still share the whole node and any number of users can be logged in at once. 

`frontendshepherd` is designed to be as simple as possible ([KISS](https://en.wikipedia.org/wiki/KISS_principle)): one Python file, one config file, one systemd unit. Furthermore, it works completely independently from any specific workload scheduling system (like SLURM or PBS, etc) and is thus compatible with any computing cluster, or even useful in other scenarios where nodes are shared among many users.

![How frontendshepherd limits CPU and memory per user](docs/flowchart.svg)

## Configuration

All settings are in `/etc/frontendshepherd.conf` and are plain numbers of CPUs, hours and GB. Check that they fit the size of your node. Restart the service after changing anything: `systemctl restart frontendshepherd`.

The most important settings are listed here. The rest (mail server, the mail texts, exempt users, the pattern for stale processes, etc) are described in comments in the config file itself.

### CPUs
| Setting | Default | |
|---|---|---|
| `min_cpus` | `2` | The number of CPUs a user can use at any given moment without draining the bucket. Usage at or below this is never throttled, and it is also used as the limit imposed on throttled users. |
| `max_cpus` | `8` | Max CPUs. The most one user can use at any moment, also for short bursts. Higher CPU utilization drains the user bucket faster. |
| `bucket_cpu_hours` | `8` | CPU bucket size in CPU hours. When a user uses more than `min_cpus`, usage starts draining the bucket (including the first `min_cpus`). When empty throttling kicks in. |
| `refill_hours` | `4` | Time for an empty bucket to refill completely. It only refills while the user's CPU usage is below the bare minimum, so not while a throttled job is still running flat out. A throttled user is only released when the bucket is full again. |

Note: A CPU is one hardware thread, as counted by `nproc` and `top`. For example, a 32 core CPU with hyperthreading is thus equivalent to having 64 "CPUs" available.

### Memory
| Setting | Default | |
|---|---|---|
| `user_max_gb` | `16` | Memory per user. Hard limit. Exceeding this limit kills all of that user's processes on the node, and other users are not affected. No page swap is ever allowed to prevent local storage filling up. |
| `reserved_gb` | `8` | Memory always unavailable to users to avoid exhausting the node. |

### Stale or long-running processes
These are handled by the "sheepdog", and its settings are in the `[sheepdog]` section of the config file.

| Setting | Default | |
|---|---|---|
| `patterns` | `\.vscode-server/` | Which processes count as stale: regular expressions matched against the full command line, separated by spaces. The default matches VS Code servers and their file watchers. |
| `max_age_days` | `7` | User processes matching a pattern are terminated after running for this long. |

### Other
| Setting | Default | |
|---|---|---|
| `dry_run` | `true` in the installed config file | Only log what would happen. Limit nothing, kill nothing and send no mail. |
| `address` | `{user}` | Where a user's mail goes, `{user}` is the login name. For example `{user}@example.org`. |
| `admin` | `root` | Gets a mail when all users together reach the total memory limit of the node. Not notified when a single user reaches a limit. |

## Example
On a frontend node with 64 CPUs and 256GB memory the defaults give each user a ceiling of 8 CPUs and 16GB memory, and all users together can use at most 248GB.

A user fully utilizing 8 CPUs is throttled after 1 hour, and if only 4 CPUs then after 2 hours, and so on. A user utilizing 2 CPUs or less will never be throttled.

Say a user accidentally starts a large `minimap2` mapping with 32 threads for several days on a frontend node. It is limited at 8 CPUs from the start, empties the bucket in 1 hour, and is then held at maximum 2 CPUs thereafter. As long as it keeps running it uses those 2 CPUs fully, so the bucket stays empty and the user stays throttled, for days if need be. Only when the job has ended, or is killed, and the user's usage is below 2 CPUs does the bucket start to refill, and 4 hours later it is full and the throttle is lifted. If the process ever exceeds the max memory limit it will be killed. In both cases the user receives an email about it being throttled or killed.

## Requirements

- Linux with cgroup v2 (the default on all current distributions)
- systemd
- Python 3.6 or later, no extra packages

## Installation

As root, on each frontend node:

    ./install.sh

It starts in **dry-run mode**: it logs what it would do, but limits nothing and sends no mail. Then:

1. Watch `journalctl -u frontendshepherd` for a few days.
2. Edit `/etc/frontendshepherd.conf`, see [Configuration](#configuration). Set at least the mail `address` and `admin`, and `smtp_host` if you do not use the local sendmail. Check mail with `frontendshepherd testmail you@example.org`.
3. Set `dry_run = false` and run `systemctl restart frontendshepherd`.

## Usage

Any user can check their own allowance:

    $ frontendshepherd status
    alice on login1
      State       ok; at this rate the bucket is empty in about 56 min
      CPU in use  5.0 CPUs (up to 8 allowed, 2 are free of charge)
      CPU bucket  59% full (4.7 of 8 CPU-hours)
      Memory      3.1 of 16 GB
      Warnings    0 in the last 7 days

`frontendshepherd status --all` lists everyone (the default for root), and `frontendshepherd status alice` shows one user.

Admins can fill a user's bucket and lift the throttle right away with `frontendshepherd reset alice`.

Everything the daemon does is logged to the systemd journal: every throttle, release, reset, process killed for memory and stale process terminated, with user and time. View it with `journalctl -u frontendshepherd`.

## How it works

Every login already runs in its own systemd cgroup, `user-UID.slice`. At startup the daemon writes the memory limits and the CPU ceiling as systemd drop-ins, which the kernel enforces even when the daemon is stopped. Every 10 seconds it reads each user's CPU counter, updates their bucket, and lowers or restores their CPU ceiling.

The CPU counter is the kernel's running total of CPU time used by all of the user's processes together. The difference between two readings, divided by the 10 seconds, is the user's average number of CPUs in that interval, so short spikes and dips are averaged out. Above the bare minimum all of the usage is taken from the bucket (not only the part above the bare minimum), below it the bucket refills, and right at it (within 5 %) nothing happens. The throttle goes on only when the bucket is completely empty and off only when it is completely full, so it can never flip back and forth quickly.

Users get an email when they are throttled or when their processes are killed for exceeding the memory limit, every time it happens (at most 100 mails per user per day). Admins are not mailed about individual users, only when the total memory limit of the node is reached. I/O is never limited and nothing is ever killed for using CPU. Root and system accounts are exempt.

There is no limit on the number of users logged in at once. Nothing is reserved per user, the limits are only ceilings, so the sum of all users' limits can be much larger than the node. The total memory limit for all users is what protects the node when many users are busy at the same time.

## Considerations

- Each frontend node keeps its own separate buckets, they are not synchronized across multiple nodes.
- Files in `/dev/shm` count as the user's memory. File cache does not get anyone killed.
- Cron jobs are covered only if cron's PAM stack uses `pam_systemd` (the usual default). Check that they show up under the user's slice in `systemd-cgls`.
- Stopping the daemon lifts all throttles; the memory limits and CPU ceiling stay. Buckets survive restarts and reboots.
- Uninstall: disable the service, then `rm /etc/systemd/system/user*.slice.d/90-frontendshepherd.conf` and `systemctl daemon-reload`.

## Test

    python3 tests/test_shepherd.py
