# FlightWall - Raspberry Pi setup

## A) Auto-start the server on boot (systemd service)

So the server launches automatically on power-up - no manual `python3` each time.

1. Find the full path to your server file and your Pi username:
   ```
   whoami
   ls ~/flightwallmini/server/flightwall_server.py     # adjust to your actual path
   ```

2. Create the service file:
   ```
   sudo nano /etc/systemd/system/flightwall.service
   ```
   Paste this (replace `admin` with your username and fix the path if different):
   ```ini
   [Unit]
   Description=FlightWall Mini server
   After=network-online.target
   Wants=network-online.target

   [Service]
   Type=simple
   User=admin
   WorkingDirectory=/home/admin/flightwallmini/server
   ExecStart=/usr/bin/python3 /home/admin/flightwallmini/server/flightwall_server.py
   Restart=always
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```
   Save: Ctrl+O, Enter, Ctrl+X.

3. Enable and start it:
   ```
   sudo systemctl daemon-reload
   sudo systemctl enable flightwall
   sudo systemctl start flightwall
   ```

4. Check it's running / see logs:
   ```
   systemctl status flightwall
   journalctl -u flightwall -f        # live log; Ctrl+C to exit
   ```

Now it starts on every boot and restarts itself if it ever crashes.
To update the code later: replace the file, then `sudo systemctl restart flightwall`.

## B) Remote access from anywhere (Tailscale)

Lets the iPhone app reach your Pi when you're away from home, over an encrypted
private network - no port forwarding, no exposing anything to the internet.

1. On the Pi:
   ```
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```
   It prints a link - open it, sign in (Google/Microsoft/GitHub), and the Pi joins
   your private "tailnet."

2. Install **Tailscale** from the App Store on your iPhone, sign in with the
   same account. Your phone and Pi can now see each other anywhere.

3. Get the Pi's Tailscale IP (starts with `100.x.x.x`):
   ```
   tailscale ip -4
   ```

4. In the FlightWall app's Device tab, set the server IP to that `100.x.x.x`
   address. Now it works on cellular, other WiFi, anywhere.

Tip: at home you can keep using the normal `192.168.x.x` IP (faster); switch to
the `100.x.x.x` one when you're out. Tailscale also gives the Pi a name like
`raspberrypi.your-tailnet.ts.net` you can use instead of the IP.
