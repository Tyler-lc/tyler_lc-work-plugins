# Setting up ixmp-copies

Steps 1-4 are once per person and machine; steps 5-8 once per project. `ixmp-copies doctor`
checks every step, here and on the cluster, and prints the fix for whatever fails; step 8 runs a
real job end to end. The examples use IIASA's UniCC cluster and H drive. For another SLURM
cluster with a shared filesystem, change the values in `ixmp_copies.toml`, not the tool.

What you need: Python 3.11 or newer with `ixmp` and `message_ix` on the workstation, Java there
for merges and transfers, a SLURM cluster with Lmod modules, and a filesystem that the
workstation and the cluster both see.

## 1. SSH to the cluster without a prompt per command

UniCC asks for your key and your password, so scripts cannot log in on their own. One shared
connection, opened interactively, serves them for eight hours. In `~/.ssh/config`:

```
Host unicc
    HostName <the cluster's login host>
    Port <its SSH port>
    User <your cluster user>
    IdentityFile ~/.ssh/<your key>
    ControlMaster auto
    ControlPath ~/.ssh/cm-unicc
    ControlPersist 8h
```

(IIASA users: the login host and port are in the UniCC user documentation.) Then, with the VPN
up, `ssh unicc` once and log in. `Permission denied (publickey,password)` from a script later
means the connection expired: log in once again.

## 2. The H drive, from both sides

Copies, seeds and job folders live on the H drive. At IIASA it is always
`/hdrive/all_users/<IIASA user>` on the cluster (`~/hdrive` there is a symlink to it). On a Linux
or WSL workstation, mount the same share. The default `[storage] roots` try
`/hdrive/all_users/<IIASA user>` first and `~/hdrive` second, so either mount it at that path
(every path then has one spelling on both sides) or anywhere else with a symlink `~/hdrive`
pointing at the mount point. The mount needs the VPN; when the VPN drops, the mount hangs or
reports `Host is down`, and the tool refuses to use it. `doctor` checks that the share it reaches
here is the one the cluster sees.

**Recommended: mounted on first access by systemd.** In `/etc/fstab`, one line:

```
//<file server>/<your home share> /mnt/iiasa/hdrive cifs credentials=/etc/.smbcredentials,uid=<uid>,gid=<gid>,_netdev,nofail,noauto,x-systemd.automount,x-systemd.idle-timeout=600 0 0
```

then

```bash
sudo mkdir -p /mnt/iiasa/hdrive
sudo systemctl daemon-reload        # after every edit of /etc/fstab
ln -s /mnt/iiasa/hdrive ~/hdrive    # where [storage] roots finds it
```

- `<file server>/<your home share>` is the UNC path Windows shows for your H drive, with `/` for
  `\`.
- `/etc/.smbcredentials` holds `username=` and `password=` lines and is readable by root only
  (`sudo chown root:root /etc/.smbcredentials && sudo chmod 600 /etc/.smbcredentials`).
- `<uid>` and `<gid>` are your local account's ids (often 1000 and 1000; check with `id`).
- `/mnt/iiasa/hdrive` is an example; mounting at `/hdrive/all_users/<IIASA user>` instead needs
  no symlink.

With `x-systemd.automount` the share mounts on first access after every reboot or VPN
reconnect, with no manual mount. On WSL this needs systemd enabled: in `/etc/wsl.conf`,

```
[boot]
systemd=true
```

then restart WSL (`wsl --shutdown` from Windows).

**Fallback: a plain mount.** The same fstab line without `noauto,x-systemd.automount,x-systemd.idle-timeout=600`
is mounted by hand: after every reboot of the machine, and after the VPN drops, run
`sudo mount /mnt/iiasa/hdrive` (or your mount point). Until then the mount point is an empty
folder, which `ls` happily lists; `doctor` reports it as not reachable.

## 3. A Python environment on the cluster

Jobs need a venv on the cluster that imports `ixmp` and `message_ix`, plus your project's own
packages on the branches the project needs. Build it with the cluster's module Python, so the
interpreter exists on every compute node:

```bash
ssh unicc
module load Python/3.11.5-GCCcore-13.2.0
python -m venv ~/repos/.venv_myproject
source ~/repos/.venv_myproject/bin/activate
pip install ixmp message_ix                  # or editable installs of your checkouts
```

Jobs load `[cluster] modules` before activating the venv: a venv built on a module Python needs
that module's shared library (`libpython3.11.so.1.0: cannot open shared object file` otherwise).

Projects that need different branches of `message-ix-models` or `message_data` need separate
venvs: a job pointed at another project's venv silently runs that project's branches. The same
holds for `message_ix` itself: venvs that import one editable checkout run whatever it holds, so
moving it for one project moves all of them. `doctor` shows the commit each venv's solves use.
Nothing of ixmp-copies is installed on the cluster: `stage` ships the tool with the code.

## 4. The tool on the workstation

Into the venv you use for the project (it needs ixmp and message_ix already):

```bash
uv pip install --python <venv>/bin/python \
    "ixmp-copies @ git+https://github.com/Tyler-lc/tyler_lc-work-plugins#subdirectory=plugins/ixmp-copies"
# or, from a clone, editable:
uv pip install --python <venv>/bin/python -e <clone>/plugins/ixmp-copies
```

`ixmp-copies --help` (or `python -m ixmp_copies --help`) lists the commands.

## 5. The project config

At the project's git repository root:

```bash
ixmp-copies init --venv '~/repos/.venv_myproject' --model MODEL --cluster-user <IIASA user>
```

`--venv` is the project's venv on the cluster (step 3), required: never another project's.
`--cluster-user` is your cluster account; without it, `init` asks `ssh unicc whoami`. It writes
`ixmp_copies.toml`, every key commented, and names the project and its platform after the folder
(`--name`, `--platform` to choose). Check `[cluster] modules` and `gams_module`
against `module avail` on the cluster, and `[storage] roots` against your mount. The folders on
the H drive default to `ixmp_copies/<project>/{test,live,backups}`. Commit the file: `stage`
ships the committed tree.

## 6. A local HyperSQL platform with CACHED tables

```bash
ixmp-copies platform-add            # dry run: what it would register
ixmp-copies platform-add --apply    # default folder ~/ixmp_local/<platform>; --dir to choose
```

This registers `[project] platform` in your ixmp config with `hsqldb.default_table_type=cached`
in its url. Without it, older ixmp versions create MEMORY tables, which hold the whole database
in the JVM and make every open slower as scenarios accumulate (the tool refuses to seed from such
a database). The database is created on first open, which may be the first `transfer` into it
(step 7); keep it on a local disk, never on the H drive. `transfer` takes a platform for a new
database only when its folder is empty, or missing below an existing folder: a url whose parent
folder is missing too (a typo, a disk not mounted) is refused. `--dir` may also name an existing
database (e.g. one made by `restore`).

## 7. Scenarios into it

```bash
ixmp-copies transfer --from ixmp-dev --to myproject-local --model MODEL --scenario SCEN          # dry run
JAVA_TOOL_OPTIONS=-Xmx16g ixmp-copies transfer --from ixmp-dev --to myproject-local \
    --model MODEL --scenario SCEN --apply
```

This adds the units, regions (with synonyms) and time slices the local platform lacks, then
clones the scenario with its solution and timeseries, and compares the copy (row counts,
timeseries, objective). A HyperSQL target is backed up first, unless its database does not exist
yet. A full MESSAGE scenario takes about ten minutes and a 16 GB heap. The same command moves
results back (`--from myproject-local --to ixmp-dev`).

## 8. Check, then try it

```bash
ixmp-copies doctor          # until nothing fails; warnings explain themselves
```

Then the acceptance trial, which builds a throwaway project with message_ix's small Dantzig model
and runs the whole chain on the cluster:
- a seed and a results main
- two solves in parallel on their own copies, and their merges
- a solve that forgets `set_as_default()`: its job fails and nothing is merged
- a solve in place of the seed's unsolved default, then a new version solved without
  `set_as_default()`: its job fails too, and nothing is merged
- a solve in place of the seed's unsolved default version: accepted and merged
- a merge cancelled on purpose, recovered with `submit_merges.sh`
- a scenario made on the workstation afterwards, merged from a newer seed
- two read jobs (no merge), one of which writes a file into its code copy
- `collect`, then `cleanup`: first the job copies that hold no run output, then, with
  `--include-outputs`, those holding GDX files or the read job's file

It needs pytest in the workstation venv (message_ix's test model imports it) and writes on the H
drive only below `ixmp_copies/trial_<time>/`:

```bash
bash <clone>/plugins/ixmp-copies/trial/new_project_trial.sh /tmp/ixc_trial '~/repos/.venv_myproject'
```

It ends with `TRIAL PASSED` and exit 0, or stops at the first step that failed.
