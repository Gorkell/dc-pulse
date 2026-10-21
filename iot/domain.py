import math
import time
import uuid


def uid():
    return uuid.uuid4().hex


class DomainError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def require(condition, message, status=400):
    if not condition:
        raise DomainError(message, status)


def ingest(db, packet, clock):
    require(isinstance(packet, dict), 'Пакет має бути об’єктом')
    sensor = db.one('SELECT s.*,e.active AS equipment_active FROM sensors s JOIN equipment e ON e.id=s.equipment_id WHERE s.id=?', (packet.get('sensor_id'),))
    require(sensor and sensor['active'] and sensor['equipment_active'], 'Датчик недоступний')
    value, at, key = packet.get('value'), packet.get('measured_at'), packet.get('packet_id')
    require(type(value) in (int, float) and math.isfinite(value), 'Некоректне значення')
    require(type(at) in (int, float) and math.isfinite(at) and 0 < at <= clock + 1, 'Некоректний час вимірювання')
    require(isinstance(key, str) and 1 <= len(key) <= 100, 'Потрібний ідентифікатор пакета')
    bounds = {'temperature': (-40, 120), 'humidity': (0, 100), 'charge': (0, 100), 'voltage': (0, 300)}
    lo, hi = bounds[sensor['metric']]
    require(lo <= value <= hi, 'Значення за фізичними межами')
    existing = db.one('SELECT * FROM measurements WHERE sensor_id=? AND (packet_id=? OR measured_at=?)', (sensor['id'], key, at))
    if existing:
        require(existing['value'] == value and existing['measured_at'] == at and existing['packet_id'] == key, 'Конфлікт дубльованого пакета', 409)
        return False
    db.execute('INSERT INTO measurements VALUES (?,?,?,?,?,?)', (uid(), sensor['id'], key, value, at, time.time()))
    return True


def forecast(points, clock, window, horizon, stale, upper):
    points = sorted([p for p in points if clock-window <= p['measured_at'] <= clock], key=lambda p:p['measured_at'])
    result = dict(available=0, value=None, slope=None, threshold_at=None, explanation='Прогноз недоступний: недостатньо історії')
    if len(points) < 6:
        return result
    if clock - points[-1]['measured_at'] > stale:
        return {**result, 'explanation': 'Прогноз недоступний: дані застарілі'}
    span = points[-1]['measured_at'] - points[0]['measured_at']
    if span < window * .5 or max(b['measured_at']-a['measured_at'] for a,b in zip(points, points[1:])) > stale:
        return {**result, 'explanation': 'Прогноз недоступний: надмірні пропуски або короткий період'}
    xs = [p['measured_at'] - clock for p in points]
    ys = [p['value'] for p in points]
    mx, my = sum(xs)/len(xs), sum(ys)/len(ys)
    variance = sum((x-mx)**2 for x in xs)
    slope = sum((x-mx)*(y-my) for x,y in zip(xs,ys))/variance
    intercept = my-slope*mx
    crossing = clock+(upper-intercept)/slope if slope>1e-8 and intercept<upper else None
    return dict(available=1, value=intercept+slope*horizon, slope=slope, threshold_at=crossing,
                explanation=f'Лінійна регресія: {len(points)} точок, {span/60:.1f} хв; зміна {slope*60:+.3f} °C/хв. Оцінка тенденції, а не передбачення поломки.')


def open_incident(db, sensor, problem, clock, forecast_id=None):
    titles = {'threshold': 'Порушення порогу', 'stale': 'Відсутні свіжі дані', 'risk': 'Прогнозований температурний ризик'}
    current = db.one("SELECT * FROM incidents WHERE sensor_id=? AND problem=? AND status<>'closed'", (sensor['id'], problem))
    if current:
        db.execute('UPDATE incidents SET updated_at=?,normal_since=NULL WHERE id=?', (clock, current['id']))
        if current['status']=='review':
            db.execute("UPDATE incidents SET status='working' WHERE id=?", (current['id'],))
            history(db,current['id'],None,clock,'review','working','Відхилення зберігається; автоматичне повернення в роботу')
        return
    incident_id = uid()
    db.execute('INSERT INTO incidents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (incident_id,sensor['equipment_id'],sensor['id'],forecast_id,problem,'high' if problem=='threshold' else 'medium','new',None,f"{titles[problem]} · {sensor['name']}",clock,clock,None))


def history(db, incident_id, author, clock, old, new, comment):
    db.execute('INSERT INTO transitions VALUES (?,?,?,?,?,?,?)', (uid(),incident_id,author,clock,old,new,comment))
    db.execute('INSERT INTO notifications VALUES (?,?,?,?)', (uid(),incident_id,clock,comment))


def invalidate_sensor(db, sensor_id, clock):
    """Start a new analysis epoch without modifying historical calculations."""
    db.execute('UPDATE sensors SET activation_epoch=activation_epoch+1,analysis_since=? WHERE id=?',(clock,sensor_id))
    db.execute("UPDATE incidents SET normal_since=NULL WHERE sensor_id=? AND status<>'closed'",(sensor_id,))


def current_forecasts(db, clock):
    """Project stored calculations into current usability; never overwrite history."""
    sensors=db.all("SELECT s.*,e.active AS equipment_active,e.name AS equipment_name,r.* FROM sensors s JOIN equipment e ON e.id=s.equipment_id JOIN rules r ON r.sensor_id=s.id WHERE s.metric='temperature' ORDER BY e.name,s.name,s.id")
    result=[]
    settings=db.one('SELECT * FROM settings WHERE id=1')
    for s in sensors:
        stored=db.one('SELECT * FROM forecasts WHERE sensor_id=? ORDER BY created_at DESC,sensor_epoch DESC,id DESC LIMIT 1',(s['id'],))
        reason=None
        if not s['equipment_active']:
            reason='Прогноз недоступний: обладнання архівоване'
        elif not s['active']:
            reason='Прогноз недоступний: датчик архівований'
        elif not stored or stored['sensor_epoch']!=s['activation_epoch']:
            reason='Прогноз недоступний: очікується новий аналіз після ввімкнення'
        elif clock-stored['created_at']>s['stale_seconds']:
            reason='Прогноз недоступний: результат аналізу застарілий'
        else:
            latest=db.one('SELECT measured_at FROM measurements WHERE sensor_id=? ORDER BY measured_at DESC LIMIT 1',(s['id'],))
            if not latest or clock-latest['measured_at']>s['stale_seconds']:
                reason='Прогноз недоступний: дані застарілі'
            elif not stored['available']:
                reason=stored['explanation']
        item=dict(stored or {},sensor_id=s['id'],name=s['name'],equipment_name=s['equipment_name'],upper_bound=s['upper_bound'],lower_bound=s['lower_bound'])
        item['historical_available']=stored['available'] if stored else 0
        item['historical_explanation']=stored['explanation'] if stored else None
        item['historical_value']=stored['value'] if stored else None
        item['available']=int(reason is None)
        item['explanation']=reason or stored['explanation']
        item.setdefault('horizon',settings['horizon'])
        if reason:
            item.update(value=None,slope=None,threshold_at=None)
        result.append(item)
    return result


def analyze(db, clock):
    settings = db.one('SELECT * FROM settings WHERE id=1')
    sensors = db.all('SELECT s.*,r.* FROM sensors s JOIN rules r ON r.sensor_id=s.id JOIN equipment e ON e.id=s.equipment_id WHERE s.active=1 AND e.active=1')
    for sensor in sensors:
        points = db.all('SELECT value,measured_at FROM measurements WHERE sensor_id=? AND measured_at>=? AND measured_at>? ORDER BY measured_at', (sensor['id'],clock-settings['analysis_window'],sensor['analysis_since']))
        latest = db.one('SELECT value,measured_at FROM measurements WHERE sensor_id=? ORDER BY measured_at DESC LIMIT 1', (sensor['id'],))
        stale = not latest or latest['measured_at']<=sensor['analysis_since'] or clock-latest['measured_at']>sensor['stale_seconds']
        bad = latest and not stale and not sensor['lower_bound']<=latest['value']<=sensor['upper_bound']
        risk = False
        forecast_available = False
        fid = None
        if sensor['metric']=='temperature':
            f = forecast(points,clock,settings['analysis_window'],settings['horizon'],sensor['stale_seconds'],sensor['upper_bound'])
            forecast_available = bool(f['available'])
            existing=db.one('SELECT id FROM forecasts WHERE sensor_id=? AND created_at=? AND sensor_epoch=?',(sensor['id'],clock,sensor['activation_epoch']))
            fid = existing['id'] if existing else uid()
            if existing:
                db.execute('UPDATE forecasts SET horizon=?,value=?,slope=?,threshold_at=?,explanation=?,available=? WHERE id=?',(settings['horizon'],f['value'],f['slope'],f['threshold_at'],f['explanation'],f['available'],fid))
            else:
                db.execute('INSERT INTO forecasts VALUES (?,?,?,?,?,?,?,?,?,?)', (fid,sensor['id'],clock,settings['horizon'],f['value'],f['slope'],f['threshold_at'],f['explanation'],f['available'],sensor['activation_epoch']))
            risk = f['available'] and not stale and not bad and f['value']>=sensor['upper_bound'] and f['slope']>0
        for problem, active in [('stale',stale),('threshold',bad),('risk',risk)]:
            if active:
                before = db.one("SELECT id FROM incidents WHERE sensor_id=? AND problem=? AND status<>'closed'", (sensor['id'],problem))
                open_incident(db,sensor,problem,clock,fid if problem=='risk' else None)
                if not before:
                    new = db.one("SELECT id FROM incidents WHERE sensor_id=? AND problem=? AND status<>'closed'", (sensor['id'],problem))
                    history(db,new['id'],None,clock,None,'new',f"Виявлено: {dict(threshold='порушення порогу',stale='втрата зв’язку',risk='прогнозний ризик')[problem]}")
            else:
                # A stale or abnormal sensor never counts as recovered, for any problem.
                normal = not stale and not bad and (problem!='risk' or (forecast_available and not risk))
                if normal:
                    db.execute("UPDATE incidents SET normal_since=COALESCE(normal_since,?) WHERE sensor_id=? AND problem=? AND status<>'closed'", (clock,sensor['id'],problem))
                else:
                    db.execute("UPDATE incidents SET normal_since=NULL WHERE sensor_id=? AND problem=? AND status<>'closed'", (sensor['id'],problem))


def transition(db, incident_id, user, action, comment, clock, assignee=None):
    incident = db.one('SELECT * FROM incidents WHERE id=?', (incident_id,))
    require(incident, 'Інцидент не знайдено',404)
    role, old = user['role'], incident['status']
    require(isinstance(comment,str) and 3<=len(comment.strip())<=2000, 'Додайте коментар (3–2000 символів)')
    if action=='assign':
        require(role=='operator','Призначення доступне оператору',403)
        require(old in ('acknowledged','working'),'Спочатку підтвердьте інцидент',409)
        engineer = db.one("SELECT id FROM users WHERE id=? AND role='engineer' AND active=1", (assignee,))
        require(engineer,'Оберіть активного інженера')
        db.execute('UPDATE incidents SET assignee_id=?,updated_at=? WHERE id=?', (assignee,clock,incident_id))
        history(db,incident_id,user['id'],clock,old,old,comment)
        return
    mapping = {'acknowledge':('new','acknowledged','operator'),'start':('acknowledged','working','engineer'),'submit':('working','review','engineer'),'close':('review','closed','operator'),'return':('review','working','operator')}
    require(action in mapping,'Невідома дія')
    expected, target, allowed = mapping[action]
    require(role==allowed,'Недостатньо прав',403)
    require(old==expected,'Недопустимий перехід стану',409)
    if role=='engineer':
        require(incident['assignee_id']==user['id'],'Інцидент призначений іншому інженеру',403)
    if action=='close':
        sensor=db.one('SELECT s.active,e.active AS equipment_active,r.* FROM sensors s JOIN equipment e ON e.id=s.equipment_id JOIN rules r ON r.sensor_id=s.id WHERE s.id=?',(incident['sensor_id'],))
        latest=db.one('SELECT * FROM measurements WHERE sensor_id=? ORDER BY measured_at DESC LIMIT 1',(incident['sensor_id'],))
        require(sensor['active'] and sensor['equipment_active'] and latest and clock-latest['measured_at']<=sensor['stale_seconds'] and sensor['lower_bound']<=latest['value']<=sensor['upper_bound'],'Для закриття потрібні актуальні нормальні показники активного датчика',409)
        recovery=db.one('SELECT recovery_seconds FROM settings WHERE id=1')['recovery_seconds']
        require(incident['normal_since'] is not None and clock-incident['normal_since']>=recovery,'Показники ще не відновлені протягом контрольного періоду',409)
    if action=='submit':
        db.execute('INSERT INTO work_records VALUES (?,?,?,?,?)', (uid(),incident_id,user['id'],clock,comment))
    db.execute('UPDATE incidents SET status=?,updated_at=? WHERE id=?', (target,clock,incident_id))
    history(db,incident_id,user['id'],clock,old,target,comment)
