# Setting up ixmp-copies

Once per person (steps 1-3), once per project (steps 4-6). Run `ixmp-copies doctor` at any
point: it checks every step below, local and on the cluster, and prints the fix for what fails.
The examples use IIASA's UniCC cluster and H drive; for another cluster change the values in
`ixmp_copies.toml`, not the tool.

## 1. SSH to the cluster without a prompt per command

UniCC asks for your key and your IIASA password, so scripts cannot log in on their own. One
shared connection, opened interactively, serves them for eight hours. In `~/.ssh/config`:

```
Host unicc
    HostName slurm-login.iiasa.ac.at
    Port 30222
    User <your IIASA user>
    IdentityFile ~/.ssh/<your key>
    ControlMaster auto
    ControlPath ~/.ssh/cm-unicc
    ControlPersist 8h
```

Then, with the VPN up, `ssh unicc` once and log in. `Permission denied (publickey,password)`
from a script later means the connection expired: log in once again.

## 2. The H drive, from both sides

Copies, seeds and job folders live on the H drive, which the workstation and the cluster both
see. On the cluster it is `/hdrive/all_users/<user>`. On a Linux or WSL workstation, mount the
same share (here at `~/hdrive`), e.g. in `/etc/fstab`:

```
//hdrive.iiasa.ac.at/home$/<uXXX>/<user> /home/<you>/hdrive cifs credentials=/home/<you>/.smbcred,uid=<uid>,gid=<gid>,_netdev,nofail 0 0
```

(`<uXXX>/<user>` is your home share's path, `.smbcred` a root-only file with `username=` and
`password=`). The mount needs the VPN; when the VPN drops, the mount hangs or reports
`Host is down`, and the tool refuses to use it.

## 3. A Python environment on the cluster

Jobs need a venv on the cluster that imports `ixmp` and `message_ix` (plus your project's own
packages, on the branches the project needs). Build it with the cluster's module Python, so the
interpreter exists on every compute node:

```bash
ssh unicc
module load Python/3.11.5-GCCcore-13.2.0
python -m venv ~/repos/.venv_myproject      # or: uv venv --python "$(which python)" ...
source ~/repos/.venv_myproject/bin/activate
pip install ixmp message_ix                  # or editable installs of your checkouts
```

Projects that need different branches of `message-ix-models` or `message_data` need separate
venvs: a job pointed at another project's venv silently runs that project's branches. The same
holds for `message_ix` itself: venvs that import one editable checkout run whatever it holds, so
moving it for one project moves all of them. `doctor` shows the commit each venv's solves use.
Nothing of ixmp-copies is installed on the cluster: `stage` ships the tool with the code.

## 4. A local HyperSQL platform with CACHED tables

Register the database under a name, with `hsqldb.default_table_type=cached` in its url. Without
it, older ixmp versions create MEMORY tables, which hold the whole database in the JVM and make
every open slower as scenarios accumulate (the tool refuses to seed from such a database).

```bash
ixmp platform add myproject-local jdbc hsqldb \
    "url=jdbc:hsqldb:file:$HOME/ixmp_local/myproject/db;hsqldb.default_table_type=cached"
```

The database is created on first open. Keep it on a local disk, not on the H drive.

## 5. Scenarios into it

```bash
ixmp-copies transfer --from ixmp-dev --to myproject-local --model MODEL --scenario SCEN          # dry run
JAVA_TOOL_OPTIONS=-Xmx16g ixmp-copies transfer --from ixmp-dev --to myproject-local \
    --model MODEL --scenario SCEN --apply
```

This adds the units, regions (with synonyms) and time slices the local platform lacks, then
clones the scenario with its solution and timeseries, and compares the copy (row counts,
timeseries, objective). A full MESSAGE scenario takes about ten minutes and a 16 GB heap. The
same command moves results back (`--from myproject-local --to ixmp-dev`).

## 6. The tool and the project config

Install the tool into the project's local venv (editable, from a clone of this repository):

```bash
uv pip install --python <venv>/bin/python -e <clone>/plugins/ixmp-copies
```

At the project's repository root:

```bash
ixmp-copies init --platform myproject-local --model MODEL --venv '~/repos/.venv_myproject'
```

Edit `ixmp_copies.toml` (areas, modules, GAMS module, heaps; every key is commented), commit it,
then `ixmp-copies doctor` until nothing fails.

For Claude Code, add the marketplace and the plugin, which brings the skill that describes the
procedure:

```
/plugin marketplace add Tyler-lc/tyler_lc-work-plugins
/plugin install ixmp-copies@tyler_lc-work-plugins
```
