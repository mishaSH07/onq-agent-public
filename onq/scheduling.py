from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo

def validate_schedule(s):
    if s['kind'] not in ('manual','daily','weekly','interval'): raise ValueError('Unknown schedule')
    ZoneInfo(s.get('timezone','America/Toronto'))
    h,m=map(int,s.get('time','19:00').split(':'))
    if not (0<=h<24 and 0<=m<60): raise ValueError('Invalid time')
    if not 0<=int(s.get('weekday',0))<=6: raise ValueError('Invalid weekday')
    if not 1<=float(s.get('hours',6))<=8760: raise ValueError('Interval must be 1–8760 hours')
    return s

def next_run(s,after=None):
    validate_schedule(s)
    after=after or datetime.now(timezone.utc)
    if s['kind']=='manual': return None
    if s['kind']=='interval': return (after+timedelta(hours=float(s['hours']))).isoformat()
    zone=ZoneInfo(s.get('timezone','America/Toronto'))
    local=after.astimezone(zone)
    hour,minute=map(int,s.get('time','19:00').split(':'))
    for day in range(9):
        date=(local+timedelta(days=day)).date()
        if s['kind']=='weekly' and date.weekday()!=int(s['weekday']): continue
        candidate=datetime(date.year,date.month,date.day,hour,minute,tzinfo=zone)
        # A nonexistent spring-forward time is normalized forward, and fall-back runs once.
        candidate=candidate.astimezone(timezone.utc)
        if candidate>after: return candidate.isoformat()
    raise ValueError('Unable to calculate next run')
