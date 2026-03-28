# Nginx

This directory holds edge proxy configuration for `project-mai-tai`.

Planned public URLs:
- `https://project-mai-tai.live`
- `https://www.project-mai-tai.live` -> redirect to apex

Control plane origin:
- `http://127.0.0.1:8100`

Expected DNS:
- `A` record for `project-mai-tai.live` -> `104.236.43.107`
- `CNAME` record for `www.project-mai-tai.live` -> `project-mai-tai.live`

Config files:
- `project-mai-tai.live.http.conf` for first-time HTTP bootstrap before the
  certificate exists
- `project-mai-tai.live.https.conf` for the final HTTPS setup after Certbot

Expected TLS flow:
1. point DNS at the VPS
2. install Nginx and Certbot
3. create the dashboard basic-auth file
4. enable `project-mai-tai.live.http.conf`
5. issue the certificate for both hostnames
6. switch to `project-mai-tai.live.https.conf`
7. verify HTTPS and the basic-auth challenge
