# Free CI: Oracle Cloud Always Free + self-hosted GitHub runner

The daily digest exhausted the 2,000 free GitHub Actions minutes/month that a
**private** repo gets. Self-hosted runner minutes are **unlimited and free**, so
we run the workflows on a free, always-on Oracle Cloud VM instead. Your laptop
is only needed for the one-time setup — after that the VM runs 24/7 in Oracle's
data center.

Nothing else changes: GitHub still schedules the jobs and still injects your
`${{ secrets.* }}` (API keys, Cloudflare token) into the run — they just execute
on your VM. All six workflows are already set to `runs-on: self-hosted`; they'll
queue until the runner below is online, then resume automatically.

---

## 1. Create the Always Free VM (~10 min, one-time)

1. Sign up at **cloud.oracle.com** (needs a card for identity check — Always
   Free resources are never charged; you can also set the account to stay on the
   free tier).
2. **Menu → Compute → Instances → Create instance.**
3. **Image and shape → Change shape → Ampere (Arm):** `VM.Standard.A1.Flex`,
   **2 OCPU / 12 GB RAM** (well within the Always Free allowance and roomy enough
   for the headless-Chrome scraping step).
   - If Ampere says *"out of capacity"*: retry in a few hours, pick a different
     Availability Domain / region, **or** use the always-free AMD shape
     `VM.Standard.E2.1.Micro` (x86, 1 GB RAM — works but tight for Chromium).
4. **Image:** Canonical **Ubuntu 22.04** (or 24.04).
5. **Add SSH keys:** *Generate a key pair for me* and **download the private
   key** (or paste your own public key).
6. Leave networking at defaults and **Create**. Note the VM's **public IP**.
   > No inbound ports are needed — the runner only makes **outbound** calls to
   > GitHub, so the default security list is fine.

## 2. SSH in and install dependencies

```bash
ssh -i /path/to/your-private-key ubuntu@<PUBLIC_IP>

# base tools + Node (for `npx wrangler` in the deploy step) + Chromium runtime libs
sudo apt-get update
sudo apt-get install -y git curl jq build-essential \
  libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
  libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
  libgbm1 libpango-1.0-0 libcairo2 libasound2
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
```

`actions/setup-python@v5` downloads Python 3.12 itself at run time (arm64 and
x64 are both supported), so nothing to do for Python.

## 3. Register the self-hosted runner

On GitHub: **repo → Settings → Actions → Runners → New self-hosted runner →
Linux**, arch **ARM64** (or **x64** if you chose the AMD shape). GitHub shows the
exact commands with a one-time token — run them on the VM. They look like:

```bash
mkdir actions-runner && cd actions-runner
# use the ARM64 URL for the Ampere shape (x64 URL for the AMD shape):
curl -o actions-runner.tar.gz -L \
  https://github.com/actions/runner/releases/download/v2.XXX.X/actions-runner-linux-arm64-2.XXX.X.tar.gz
tar xzf actions-runner.tar.gz
./config.sh --url https://github.com/shubham1108research-stack/quant-digest \
  --token <TOKEN_FROM_GITHUB>
# press Enter at the prompts to accept the default name, the `self-hosted` label,
# and the _work folder
```

## 4. Run it as a service (auto-start on boot, survives reboots)

```bash
sudo ./svc.sh install
sudo ./svc.sh start
sudo ./svc.sh status        # should show "active (running)"
```

The runner now polls GitHub forever. **Settings → Actions → Runners** should show
it green/**Idle**.

## 5. Confirm it works

- **Actions → Deploy Portal → Run workflow** (the lightest job). It should pick up
  on your VM within seconds and go green — with **no minutes billed**.
- The daily digest (`Research Digest`) will then run on the VM on its normal
  schedule; the external cron-job.org trigger keeps working unchanged.

---

## Notes & gotchas

- **Secrets:** unchanged. GitHub injects the repo secrets into the run on the
  self-hosted runner exactly as before — no keys stored on the VM.
- **Keep it "active":** Oracle may reclaim *idle* Always Free compute. A daily
  digest run keeps it active enough; if you want insurance, a tiny cron like
  `* * * * * uptime >/dev/null` is plenty.
- **Playwright deps:** the digest runs `playwright install --with-deps chromium`;
  `--with-deps` uses `sudo apt` and the default `ubuntu` user has passwordless
  sudo, so it just works. (Step 2 also pre-installs the libs as a belt-and-braces.)
- **Disk:** the runner reuses its `_work` dir; `git`/`pip` caches grow slowly.
  `df -h` occasionally; the Always Free boot volume is ~50 GB, ample.
- **Reverting to GitHub-hosted** (e.g. after minutes reset, or to test): change
  `runs-on: self-hosted` back to `runs-on: ubuntu-latest` in
  `.github/workflows/*.yml`. You can even keep both with
  `runs-on: [self-hosted, linux]` as a label filter later.
- **Updates:** the runner self-updates. If the service ever dies,
  `sudo ./svc.sh start` from `~/actions-runner`.
