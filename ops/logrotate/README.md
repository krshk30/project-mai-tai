# Application log retention

`project-mai-tai` services write to `/var/log/project-mai-tai/*.log`. The host's
daily `logrotate.timer` rotates those files.

## Policy

Keep 30 daily rotations of the complete application logs. The previous seven
rotations erased 18 of 19 duplicate cases before the question was asked and put
the 2026-08-21 Webull combo replies on a 2026-08-29 deadline. The longest
observed evidence-discovery interval was 21 days (2026-08-03 to 2026-08-24), so
30 days supplies nine days of margin.

This intentionally does not export a selected marker list. A selected export is
smaller only by creating a second observability schema whose omissions are
silent. On 2026-08-25 the entire application-log directory was 53 MB, compressed
rotations were 6.3 MB, and the VPS had 88 GB free.

The longer window also retains credentials or account details accidentally
written to logs for longer. Existing access remains `root:root 0640`, and the
window remains bounded at 30 rotations.

## Automatic installation

The `Reconcile Log Retention` workflow runs every day at `22:30 UTC`, which is
`17:30 ET` in standard time and `18:30 ET` in daylight time. It also checks the
live Eastern clock and refuses the weekday `07:00-16:00 ET` market window, so a
delayed scheduled run cannot silently become a live-session change. A manual
dispatch remains available after hours.

The workflow checks out the reviewed repository revision on its GitHub runner,
copies only `install.sh` and the policy into an isolated VPS `/tmp` directory,
and invokes the installer from there. It does **not** pull the application
checkout, update its virtual environment, run migrations, or restart any
application service. Log retention therefore cannot block or partially apply an
application deployment.

The installer:

1. makes logrotate parse the candidate and proves it saw the application glob;
2. compares the reviewed policy with the installed bytes and replaces it only
   when they differ;
3. enables and starts the host's daily `logrotate.timer`;
4. verifies the timer is enabled and active and the installed bytes match.

Any failed step makes the dedicated workflow red and the next daily run retries.
It does not prevent an emergency application deployment. No application service
is restarted merely to rotate a log.

`copytruncate` remains necessary because systemd holds each append target open.
Its small copy/truncate race can lose lines written during a rotation; this
change extends retained history but does not remove that pre-existing race.

Run the non-production controls with:

```bash
bash ops/logrotate/test_install.sh
```
