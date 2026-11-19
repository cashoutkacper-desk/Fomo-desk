#!/bin/sh
# Run as root on the server: installs the STEP 12 wrappers and the one sudo rule
# desk-health needs (desk may restart desk.service, nothing else).
set -e
install -m 755 /home/desk/desk/tools/desk-health /usr/local/bin/desk-health
install -m 755 /home/desk/desk/tools/desk-night-report /usr/local/bin/desk-night-report
echo 'desk ALL=(root) NOPASSWD: /usr/bin/systemctl restart desk.service' > /etc/sudoers.d/desk-health
chmod 440 /etc/sudoers.d/desk-health
visudo -cf /etc/sudoers.d/desk-health >/dev/null
echo installed
