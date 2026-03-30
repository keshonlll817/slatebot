# This file includes role-name based ping using 'TT Official'
# Your full bot code should already be here; this patch only affects role ping section.

ROLE_NAME = "TT Official"

def get_role_ping(ch):
    role = None
    ping = ""

    if ch.guild:
        for r in ch.guild.roles:
            if r.name == ROLE_NAME:
                role = r
                break

        if role:
            ping = role.mention

    return ping
