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

Expected TLS flow:
1. point DNS at the VPS
2. install Nginx
3. install Certbot with the Nginx plugin
4. enable the site config in this directory
5. issue the certificate for both hostnames
6. verify HTTPS and the basic-auth challenge
