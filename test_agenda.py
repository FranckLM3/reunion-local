"""Vérification du choix d'événement et des invités : .venv/bin/python test_agenda.py"""

from datetime import datetime

from agenda import evenement

ICS = """BEGIN:VCALENDAR
VERSION:2.0
X-WR-CALNAME:moi@example.com
X-WR-TIMEZONE:Europe/Paris
BEGIN:VEVENT
UID:hebdo
DTSTART;TZID=Europe/Paris:20260907T103000
DTEND;TZID=Europe/Paris:20260907T113000
RRULE:FREQ=WEEKLY;BYDAY=MO
SUMMARY:Point hebdo
X-GOOGLE-CONFERENCE:https://meet.google.com/abc-defg-hij
ORGANIZER;CN=Lou:mailto:moi@example.com
ATTENDEE;CN=Lou;PARTSTAT=ACCEPTED:mailto:moi@example.com
ATTENDEE;CN=Alex Martin;PARTSTAT=ACCEPTED:mailto:antoine@example.com
ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:sacha@exemple.com
ATTENDEE;CN=Dom;PARTSTAT=DECLINED:mailto:jm@example.com
ATTENDEE;CUTYPE=RESOURCE;CN=Salle Brest:mailto:salle@resource.calendar.google.com
END:VEVENT
BEGIN:VEVENT
UID:autre
DTSTART;TZID=Europe/Paris:20261005T102500
DTEND;TZID=Europe/Paris:20261005T110000
SUMMARY:Rappel sans visio
END:VEVENT
BEGIN:VEVENT
UID:jour
DTSTART;VALUE=DATE:20261005
DTEND;VALUE=DATE:20261006
SUMMARY:Journée entière
END:VEVENT
END:VCALENDAR
""".encode()

# Lundi 05/10/2026, enregistrement lancé à 10h34 : occurrence du point hebdo, préférée car elle a un Meet.
r = evenement(ICS, datetime(2026, 10, 5, 10, 34))
assert r == {"titre": "Point hebdo", "noms": ["Alex Martin", "sacha"]}, r
# Mardi à 10h34 : rien (le point hebdo est le lundi, la journée entière ne compte pas).
assert evenement(ICS, datetime(2026, 10, 6, 10, 34)) is None
# Lundi à 14h : trop loin de tout événement.
assert evenement(ICS, datetime(2026, 10, 5, 14, 0)) is None
print("ok")
