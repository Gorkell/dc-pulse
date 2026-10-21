import math
import os
import threading
import time
from werkzeug.security import generate_password_hash
from .db import DB
from .domain import uid, ingest, analyze

SCENARIOS = {'normal','warming','overheat','disconnect','power','recovery'}


def target_error(db, sensor_id, metric, kind):
    sensor=db.one('SELECT s.*,e.kind,e.active AS equipment_active FROM sensors s JOIN equipment e ON e.id=s.equipment_id WHERE s.id=?',(sensor_id,))
    if not sensor:
        return 'Ціль не вибрана або відсутня. Оберіть датчик на екрані «Демонстрація».'
    if not sensor['active'] or not sensor['equipment_active']:
        return 'Ціль архівована. Увімкніть її або оберіть інший активний датчик.'
    if sensor['metric']!=metric or sensor['kind']!=kind:
        return 'Тип цілі змінено. Оберіть датчик відповідного обладнання.'
    return None


def targets_state(db):
    targets=db.one('SELECT * FROM simulation_targets WHERE id=1') or dict(temperature_sensor_id=None,power_sensor_id=None)
    return {**targets,'temperature_error':target_error(db,targets['temperature_sensor_id'],'temperature','rack'),
            'power_error':target_error(db,targets['power_sensor_id'],'charge','ups')}


def seed(db):
    if db.one('SELECT id FROM settings WHERE id=1'):
        return
    password = os.environ.get('DEMO_PASSWORD', 'Demo-IoT-2026!')
    if os.environ.get('COOKIE_SECURE')=='1' and not os.environ.get('DEMO_PASSWORD'):
        raise RuntimeError('Set DEMO_PASSWORD before production seeding')
    for role, name in [('operator','Шерман Максим'),('engineer','Андрій Бондар'),('admin','Адміністратор'),('manager','Керівник')]:
        if not db.one('SELECT id FROM users WHERE username=?',(role,)):
            db.execute('INSERT INTO users VALUES (?,?,?,?,?,1)', (uid(),role,name,role,generate_password_hash(password)))
    clock=time.time()
    db.execute('INSERT INTO settings VALUES (1,1800,900,120)')
    db.execute("INSERT INTO simulation VALUES (1,'normal',0,1,?,0,NULL,0)", (clock,))
    db.execute('INSERT INTO simulation_targets (id) VALUES (1) ON CONFLICT DO NOTHING')
    for i in range(1,9):
        kind='rack' if i<=6 else 'ups'
        eid=uid()
        db.execute('INSERT INTO equipment VALUES (?,?,?,?,1)', (eid,f'R-{i:02}' if kind=='rack' else f'UPS-{i-6:02}',kind,f'Зала A · ряд {(i-1)//3+1}'))
        metrics = [('temperature','Температура','°C',18,32),('humidity','Вологість','%',25,65)] if kind=='rack' else [('charge','Заряд батареї','%',30,100),('voltage','Напруга','В',210,245),('temperature','Температура','°C',15,40)]
        for metric,label,unit,lo,hi in metrics:
            sid=uid()
            db.execute('INSERT INTO sensors (id,equipment_id,name,metric,unit,active) VALUES (?,?,?,?,?,1)', (sid,eid,label,metric,unit))
            if i==1 and metric=='temperature':
                db.execute('UPDATE simulation_targets SET temperature_sensor_id=? WHERE id=1',(sid,))
            if i==7 and metric=='charge':
                db.execute('UPDATE simulation_targets SET power_sensor_id=? WHERE id=1',(sid,))
            db.execute('INSERT INTO rules VALUES (?,?,?,?)', (sid,lo,hi,180))
            for n in range(61):
                at=clock-(60-n)*30
                base={'temperature':24+i*.25,'humidity':44,'charge':98,'voltage':230}[metric]
                value=base+math.sin(n/6+i)*(.25 if metric=='temperature' else 1)
                ingest(db,dict(sensor_id=sid,value=value,measured_at=at,packet_id=f'seed-{n}'),clock)
    analyze(db,clock)


def tick(db, owner, wall=None):
    wall=wall or time.time()
    sim=db.one('SELECT * FROM simulation WHERE id=1')
    if not sim or (sim['lease_until']>wall and sim['lease_owner']!=owner):
        return False
    db.execute('UPDATE simulation SET lease_owner=?,lease_until=? WHERE id=1', (owner,wall+12))
    if not sim['running']:
        return False
    targets=targets_state(db)
    error=targets['temperature_error'] if sim['scenario'] in ('warming','overheat','disconnect') else targets['power_error'] if sim['scenario']=='power' else None
    if error:
        db.execute('UPDATE simulation SET running=0 WHERE id=1')
        return False
    clock=sim['clock']+2*sim['speed']
    step=sim['step']+1
    scenario=sim['scenario']
    sensors=db.all('SELECT s.* FROM sensors s JOIN equipment e ON e.id=s.equipment_id WHERE s.active=1 AND e.active=1 ORDER BY s.id')
    for s in sensors:
        target=s['id']==targets.get('temperature_sensor_id')
        if scenario=='disconnect' and target and s['metric']=='temperature':
            continue
        previous=db.one('SELECT value FROM measurements WHERE sensor_id=? ORDER BY measured_at DESC LIMIT 1', (s['id'],))
        base={'temperature':25,'humidity':44,'charge':98,'voltage':230}[s['metric']]
        desired=base+math.sin(step/10)*(.3 if s['metric']=='temperature' else .8)
        value=previous['value'] if previous else base
        if scenario=='warming' and target and s['metric']=='temperature':
            value=min(45,value+2*sim['speed']*.012)
        elif scenario=='overheat' and target and s['metric']=='temperature':
            value=min(40,value+2*sim['speed']*.15)
        elif scenario=='power' and s['id']==targets.get('power_sensor_id') and s['metric']=='charge':
            value=max(15,value-2*sim['speed']*.12)
        else:
            value+=max(-2*sim['speed']*.08,min(2*sim['speed']*.08,(desired-value)*.35))
        ingest(db,dict(sensor_id=s['id'],value=round(value,3),measured_at=clock,packet_id=f'tick-{clock:.6f}-{s["id"]}'),clock)
    db.execute('UPDATE simulation SET clock=?,step=? WHERE id=1', (clock,step))
    analyze(db,clock)
    return True


def start_worker(url):
    owner=uid()
    stop=threading.Event()
    def loop():
        import logging
        while not stop.wait(2):
            try:
                with DB(url) as db:
                    db.lock()
                    tick(db,owner)
            except Exception:
                logging.exception('Simulator tick failed')
    threading.Thread(target=loop,daemon=True,name='iot-simulator').start()
    return stop
