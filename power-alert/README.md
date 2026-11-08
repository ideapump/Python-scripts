# power-alert

Logs into your electricity provider, reads your daily kWh, and posts a GroupMe
update telling you whether you're on track for your goal (default 1050 kWh,
safely over the 1000 kWh bonus).

## 1. Make the GroupMe bot
1. Go to https://dev.groupme.com/bots and sign in.
2. Click **Create Bot**, pick the group it should post in, and give it a name. Leave the callback URL blank.
3. Copy the **Bot ID**.

## 2. Set it up (works the same on your Mac for testing and on the Linux box)
Needs Python 3.11 or newer.

```bash
mkdir -p ~/power-alert && cd ~/power-alert      # put the files here
python3 -m venv .venv
.venv/bin/pip install playwright
.venv/bin/playwright install --with-deps chromium   # on Mac: drop --with-deps
cp config.example.toml config.toml && chmod 600 config.toml
```

Edit `config.toml`: your login, the GroupMe bot ID, the day your billing cycle
starts, and the page selectors (see step 3).

Test the bot and preview the message with fake data:
```bash
.venv/bin/python power_alert.py --test-groupme
.venv/bin/python power_alert.py --demo
```

## 3. Point it at your provider's site
Every utility site is different. To find the right settings:
1. Log in in a normal browser, open the usage page, and switch it to **daily** view as a table if possible.
2. Copy that page's address into `usage_url`.
3. Right-click a day's row and choose **Inspect** to find selectors for the row and which column holds the date and the kWh.
4. Do the same for the username box, password box and sign-in button on the login page.

Then run it with the browser visible (easiest on your Mac):
```bash
.venv/bin/python power_alert.py --headed --debug --dry-run
```
`--debug` saves a screenshot, the page HTML and the site's JSON responses into
`debug/` so you can see what it got. If your site asks for a 2FA code, enter it
in that visible window once and tick "remember this device". The login is
saved in `browser-profile/`.

## 4. Run it automatically on Linux
```bash
mkdir -p ~/.config/systemd/user
cp power-alert.service power-alert.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now power-alert.timer
sudo loginctl enable-linger "$USER"     # keeps it running when you're logged out
```
It checks six times a day and posts only when a new day of data appears, so
you get one message a day. If it breaks (password changed, site redesign), the
bot tells you once that day.

Useful commands:
```bash
systemctl --user start power-alert.service     # run now
journalctl --user -u power-alert -n 50         # see logs
systemctl --user list-timers                   # see next run
```

## Options
| Flag | What it does |
|---|---|
| `--dry-run` | Print the message instead of posting it |
| `--force` | Post even if there's no new day |
| `--debug` | Save screenshots, HTML and JSON to `debug/` |
| `--headed` | Show the browser window |
| `--demo` | Fake data, no website, nothing posted |
| `--test-groupme` | Send a test message |
