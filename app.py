import csv
import io
import logging
import math
import os
import secrets
import time
from functools import wraps
from pathlib import Path
from flask import Flask, request, jsonify, g, send_from_directory, Response
from werkzeug.security import check_password_hash, generate_password_hash
from iot.db import DB, ROOT, migrate
from iot.domain import DomainError, require, uid, ingest, analyze, transition, invalidate_sensor, current_forecasts
from iot.simulator import seed, start_worker, SCENARIOS, targets_state, target_error


def load_env():
    path=ROOT/'.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.strip().startswith('#'):
                key,value=line.split('=',1)
                os.environ.setdefault(key.strip(),value.strip())


def create_app(database_url=None, worker=False):
    load_env()
    app=Flask(__name__,static_folder='static')
    app.config['MAX_CONTENT_LENGTH']=64*1024
    url=database_url or os.environ.get('DATABASE_URL',f'sqlite:///{ROOT / "data.sqlite3"}')
    app.config['DATABASE_URL']=url
    migrate(url)
    with DB(url) as db:
        db.lock()
        seed(db)
    login_attempts={}

    def access(*roles):
        def decorate(fn):
            @wraps(fn)
            def wrapped(*args,**kwargs):
                token=request.cookies.get('iot_session','')
                import hashlib
                db=DB(url)
                g.db=db
                user=db.one('SELECT u.*,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>? AND u.active=1', (hashlib.sha256(token.encode()).hexdigest(),time.time()))
                require(user,'Потрібна авторизація',401)
                g.user=user
                if roles:
                    require(user['role'] in roles,'Недостатньо прав',403)
                if request.method not in ('GET','HEAD','OPTIONS'):
                    require(secrets.compare_digest(request.headers.get('X-CSRF-Token',''),user['csrf']),'Недійсний CSRF-токен',403)
                    db.lock()
                return fn(*args,**kwargs)
            return wrapped
        return decorate

    @app.teardown_request
    def teardown(error):
        if hasattr(g,'db'):
            g.db.conn.rollback() if error or getattr(g,'failed',False) else g.db.conn.commit()
            g.db.close()

    @app.errorhandler(DomainError)
    def domain_error(error):
        g.failed=True
        return jsonify(error=error.message),error.status

    @app.errorhandler(500)
    def internal_error(error):
        g.failed=True
        return jsonify(error='Помилка сервера. Перегляньте журнал застосунку.'),500

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        if request.path.startswith('/api/'):
            response.headers['Cache-Control']='no-store'
        return response

    def body():
        data=request.get_json(silent=True)
        require(isinstance(data,dict),'Очікується JSON-об’єкт')
        return data

    def integer(data,key,lo,hi):
        value=data.get(key)
        require(type(value)==int and lo<=value<=hi,f'Некоректне поле: {key}')
        return value

    def number(data,key):
        value=data.get(key)
        require(type(value) in (int,float) and math.isfinite(value),f'Некоректне поле: {key}')
        return value

    def text(data,key,limit=120):
        value=data.get(key)
        require(isinstance(value,str) and 1<=len(value.strip())<=limit,f'Некоректне поле: {key}')
        return value.strip()

    def clock():
        return g.db.one('SELECT clock FROM simulation WHERE id=1')['clock']

    def visible_incidents():
        sql='SELECT i.*,e.name AS equipment_name,u.name AS assignee_name FROM incidents i JOIN equipment e ON e.id=i.equipment_id LEFT JOIN users u ON u.id=i.assignee_id'
        if g.user['role']=='engineer':
            return g.db.all(sql+' WHERE i.assignee_id=? ORDER BY i.updated_at DESC',(g.user['id'],))
        return g.db.all(sql+' ORDER BY i.updated_at DESC')

    @app.get('/')
    def index():
        return send_from_directory('static','index.html')

    @app.get('/health')
    def health():
        with DB(url) as db:
            db.one('SELECT version FROM schema_migrations')
        return jsonify(status='ok')

    @app.post('/api/login')
    def login():
        data=body()
        remote=request.remote_addr
        now=time.time()
        attempts=[t for t in login_attempts.get(remote,[]) if t>now-60]
        require(len(attempts)<15,'Забагато спроб. Зачекайте хвилину.',429)
        login_attempts[remote]=attempts+[now]
        with DB(url) as db:
            user=db.one('SELECT * FROM users WHERE username=? AND active=1',(text(data,'username'),))
            password=data.get('password','')
            require(isinstance(password,str) and len(password)<=256,'Некоректний пароль')
            require(user and check_password_hash(user['password_hash'],password),'Невірний логін або пароль',401)
            token,csrf=secrets.token_urlsafe(32),secrets.token_urlsafe(24)
            import hashlib
            db.execute('DELETE FROM sessions WHERE expires<?',(now,))
            db.execute('INSERT INTO sessions VALUES (?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),user['id'],csrf,now+8*3600))
            response=jsonify(user={k:user[k] for k in ('id','name','role','username')},csrf=csrf)
            response.set_cookie('iot_session',token,httponly=True,samesite='Strict',secure=os.environ.get('COOKIE_SECURE')=='1',max_age=8*3600)
            return response

    @app.get('/api/me')
    @access()
    def me():
        return jsonify(user={k:g.user[k] for k in ('id','name','role','username')},csrf=g.user['csrf'])

    @app.post('/api/logout')
    @access()
    def logout():
        g.db.execute('DELETE FROM sessions WHERE user_id=?',(g.user['id'],))
        response=jsonify(ok=True)
        response.delete_cookie('iot_session')
        return response

    @app.get('/api/state')
    @access()
    def state():
        db=g.db
        equipment=db.all('SELECT * FROM equipment ORDER BY name')
        sensors=db.all('SELECT s.*,r.*,m.value,m.measured_at,m.received_at FROM sensors s JOIN rules r ON r.sensor_id=s.id LEFT JOIN measurements m ON m.id=(SELECT id FROM measurements WHERE sensor_id=s.id ORDER BY measured_at DESC LIMIT 1) ORDER BY s.name')
        now=clock()
        for s in sensors:
            equipment_active=next(e['active'] for e in equipment if e['id']==s['equipment_id'])
            s['equipment_active']=equipment_active
            s['state']='inactive' if not s['active'] or not equipment_active else 'stale' if s['measured_at'] is None or s['measured_at']<=s['analysis_since'] or now-s['measured_at']>s['stale_seconds'] else 'alarm' if not s['lower_bound']<=s['value']<=s['upper_bound'] else 'normal'
        notifications=db.all('SELECT n.*,r.user_id AS read_by FROM notifications n LEFT JOIN notification_reads r ON r.notification_id=n.id AND r.user_id=? '+('JOIN incidents i ON i.id=n.incident_id WHERE i.assignee_id=? ' if g.user['role']=='engineer' else '')+'ORDER BY n.created_at DESC LIMIT 30', (g.user['id'],g.user['id']) if g.user['role']=='engineer' else (g.user['id'],))
        return jsonify(equipment=equipment,sensors=sensors,incidents=visible_incidents(),simulation=db.one('SELECT id,scenario,running,speed,clock,step FROM simulation'),targets=targets_state(db),settings=db.one('SELECT * FROM settings'),users=db.all('SELECT id,username,name,role,active FROM users ORDER BY name'),notifications=notifications,real_time=time.time())

    def filtered_measurements():
        sensor_id=request.args.get('sensor_id','')
        require(g.db.one('SELECT id FROM sensors WHERE id=?',(sensor_id,)),'Оберіть датчик')
        try:
            start=float(request.args.get('from',clock()-3600))
            end=float(request.args.get('to',clock()))
        except ValueError:
            raise DomainError('Некоректний період')
        require(math.isfinite(start) and math.isfinite(end) and start<=end,'Некоректний період')
        return g.db.all('SELECT m.*,s.name AS sensor_name,s.unit FROM measurements m JOIN sensors s ON s.id=m.sensor_id WHERE sensor_id=? AND measured_at BETWEEN ? AND ? ORDER BY measured_at DESC LIMIT 10000',(sensor_id,start,end))

    @app.get('/api/measurements')
    @access()
    def measurements():
        return jsonify(items=filtered_measurements(),limit=10000)

    @app.post('/api/measurements')
    @access('admin')
    def receive():
        added=ingest(g.db,body(),clock())
        analyze(g.db,clock())
        return jsonify(inserted=added)

    @app.get('/api/forecasts')
    @access()
    def forecasts():
        return jsonify(items=current_forecasts(g.db,clock()))

    @app.get('/api/incidents/<iid>')
    @access()
    def incident(iid):
        item=next((i for i in visible_incidents() if i['id']==iid),None)
        require(item,'Інцидент недоступний',404)
        return jsonify(item=item,history=g.db.all('SELECT t.*,u.name AS author FROM transitions t LEFT JOIN users u ON u.id=t.author_id WHERE incident_id=? ORDER BY created_at,id',(iid,)),work=g.db.all('SELECT w.*,u.name AS author FROM work_records w JOIN users u ON u.id=w.author_id WHERE incident_id=? ORDER BY created_at',(iid,)))

    @app.post('/api/incidents/<iid>/action')
    @access('operator','engineer')
    def action(iid):
        data=body()
        analyze(g.db,clock())
        transition(g.db,iid,g.user,data.get('action'),data.get('comment'),clock(),data.get('assignee_id'))
        return jsonify(ok=True)

    @app.post('/api/equipment')
    @access('admin')
    def equipment_save():
        data=body()
        name,location=text(data,'name'),text(data,'location')
        kind=data.get('kind')
        require(kind in ('rack','ups'),'Невідомий тип обладнання')
        active=integer(data,'active',0,1)
        existing=g.db.one('SELECT id FROM equipment WHERE name=?',(name,))
        require(not existing or existing['id']==data.get('id'),'Назва вже існує',409)
        if data.get('id'):
            previous=g.db.one('SELECT * FROM equipment WHERE id=?',(data['id'],))
            require(previous,'Обладнання не знайдено',404)
            if previous['active']!=active:
                for s in g.db.all('SELECT id FROM sensors WHERE equipment_id=?',(data['id'],)):
                    invalidate_sensor(g.db,s['id'],clock())
            g.db.execute('UPDATE equipment SET name=?,kind=?,location=?,active=? WHERE id=?',(name,kind,location,active,data['id']))
        else:
            g.db.execute('INSERT INTO equipment VALUES (?,?,?,?,?)',(uid(),name,kind,location,active))
        return jsonify(ok=True)

    @app.post('/api/sensors')
    @access('admin')
    def sensor_save():
        data=body()
        metric=data.get('metric')
        units={'temperature':'°C','humidity':'%','charge':'%','voltage':'В'}
        require(metric in units,'Невідома метрика')
        require(g.db.one('SELECT id FROM equipment WHERE id=?',(data.get('equipment_id'),)),'Обладнання не знайдено')
        sid=data.get('id') or uid()
        name=text(data,'name')
        active=integer(data,'active',0,1)
        lo,hi=number(data,'lower_bound'),number(data,'upper_bound')
        physical={'temperature':(-40,120),'humidity':(0,100),'charge':(0,100),'voltage':(0,300)}[metric]
        require(physical[0]<=lo<hi<=physical[1],'Пороги за фізичними межами')
        stale=integer(data,'stale_seconds',30,3600)
        existing=g.db.one('SELECT * FROM sensors WHERE id=?',(sid,))
        if existing:
            require(existing['metric']==metric and existing['equipment_id']==data['equipment_id'],'Метрику та обладнання історичного датчика змінювати не можна')
            if existing['active']!=active:
                invalidate_sensor(g.db,sid,clock())
            g.db.execute('UPDATE sensors SET name=?,active=? WHERE id=?',(name,active,sid))
            g.db.execute('UPDATE rules SET lower_bound=?,upper_bound=?,stale_seconds=? WHERE sensor_id=?',(lo,hi,stale,sid))
        else:
            g.db.execute('INSERT INTO sensors (id,equipment_id,name,metric,unit,active) VALUES (?,?,?,?,?,?)',(sid,data['equipment_id'],name,metric,units[metric],active))
            g.db.execute('INSERT INTO rules VALUES (?,?,?,?)',(sid,lo,hi,stale))
        analyze(g.db,clock())
        return jsonify(ok=True)

    @app.post('/api/users')
    @access('admin')
    def user_save():
        data=body()
        role=data.get('role')
        require(role in ('operator','engineer','admin','manager'),'Невідома роль')
        name,username=text(data,'name'),text(data,'username',50)
        active=integer(data,'active',0,1)
        require(data.get('id')!=g.user['id'],'Власний обліковий запис змінити не можна')
        existing=g.db.one('SELECT id FROM users WHERE username=?',(username,))
        require(not existing or existing['id']==data.get('id'),'Логін вже існує',409)
        password=data.get('password','')
        require(isinstance(password,str) and len(password)<=256,'Некоректний пароль')
        if password or not data.get('id'):
            require(len(password)>=12,'Пароль: щонайменше 12 символів')
        if data.get('id'):
            require(g.db.one('SELECT id FROM users WHERE id=?',(data['id'],)),'Користувача не знайдено',404)
            assigned=g.db.one("SELECT id FROM incidents WHERE assignee_id=? AND status<>'closed'",(data['id'],))
            require(not assigned or (role=='engineer' and active),'Спочатку перепризначте відкриті інциденти')
            g.db.execute('UPDATE users SET username=?,name=?,role=?,active=? WHERE id=?',(username,name,role,active,data['id']))
            if password:
                g.db.execute('UPDATE users SET password_hash=? WHERE id=?',(generate_password_hash(password),data['id']))
            g.db.execute('DELETE FROM sessions WHERE user_id=?',(data['id'],))
        else:
            g.db.execute('INSERT INTO users VALUES (?,?,?,?,?,?)',(uid(),username,name,role,generate_password_hash(password),active))
        return jsonify(ok=True)

    @app.post('/api/settings')
    @access('admin')
    def settings_save():
        data=body()
        g.db.execute('UPDATE settings SET analysis_window=?,horizon=?,recovery_seconds=? WHERE id=1',(integer(data,'analysis_window',300,86400),integer(data,'horizon',60,7200),integer(data,'recovery_seconds',30,3600)))
        analyze(g.db,clock())
        return jsonify(ok=True)

    @app.post('/api/simulation')
    @access('operator','admin')
    def simulation():
        data=body()
        require(data.get('scenario') in SCENARIOS,'Невідомий сценарій')
        running=integer(data,'running',0,1)
        speed=data.get('speed')
        require(type(speed)==int and speed in (1,5,15,30),'Недопустима швидкість')
        targets=targets_state(g.db)
        error=targets['temperature_error'] if data['scenario'] in ('warming','overheat','disconnect') else targets['power_error'] if data['scenario']=='power' else None
        require(not running or not error,error or 'Ціль недоступна',409)
        g.db.execute('UPDATE simulation SET scenario=?,running=?,speed=? WHERE id=1',(data['scenario'],running,speed))
        return jsonify(ok=True)

    @app.post('/api/simulation/targets')
    @access('operator','admin')
    def save_targets():
        data=body()
        values=[]
        for key,metric,kind in [('temperature_sensor_id','temperature','rack'),('power_sensor_id','charge','ups')]:
            sid=data.get(key) or None
            require(sid is None or isinstance(sid,str),'Некоректний ідентифікатор цілі')
            if sid:
                error=target_error(g.db,sid,metric,kind)
                require(not error,error or 'Ціль недоступна')
            values.append(sid)
        g.db.execute('UPDATE simulation_targets SET temperature_sensor_id=?,power_sensor_id=? WHERE id=1',values)
        sim=g.db.one('SELECT scenario FROM simulation WHERE id=1')
        if (sim['scenario'] in ('warming','overheat','disconnect') and not values[0]) or (sim['scenario']=='power' and not values[1]):
            g.db.execute('UPDATE simulation SET running=0 WHERE id=1')
        return jsonify(ok=True)

    @app.post('/api/demo/reset')
    @access('admin')
    def reset():
        require(body().get('confirmation')=='СКИНУТИ ДЕМО','Потрібне підтвердження скидання')
        for table in ('notification_reads','notifications','transitions','work_records','incidents','forecasts','measurements','rules','simulation_targets','sensors','equipment','simulation','settings'):
            g.db.execute(f'DELETE FROM {table}')
        # Keep users and sessions; only reseed demo infrastructure.
        g.db.execute('UPDATE users SET active=active')
        seed_demo_without_users(g.db)
        return jsonify(ok=True)

    @app.post('/api/notifications/read')
    @access()
    def read_notifications():
        for n in g.db.all('SELECT id FROM notifications'):
            g.db.execute('INSERT INTO notification_reads VALUES (?,?) ON CONFLICT DO NOTHING',(n['id'],g.user['id']))
        return jsonify(ok=True)

    @app.get('/api/export/<kind>')
    @access()
    def export(kind):
        require(kind in ('measurements','incidents'),'Невідомий експорт',404)
        if kind=='measurements':
            items=filtered_measurements()
            fields=['sensor_name','value','unit','measured_at','received_at']
            labels=['Датчик','Значення','Одиниця','Час вимірювання UTC','Час отримання UTC']
        else:
            items=visible_incidents()
            status=request.args.get('status')
            if status:
                items=[i for i in items if i['status']==status]
            query=request.args.get('q','').casefold()
            items=[i for i in items if query in (i['title']+' '+i['equipment_name']).casefold()]
            fields=['title','equipment_name','status','priority','assignee_name','created_at','updated_at']
            labels=['Інцидент','Обладнання','Стан','Пріоритет','Виконавець','Створено UTC','Оновлено UTC']
        buffer=io.StringIO()
        writer=csv.writer(buffer)
        writer.writerow(labels)
        from datetime import datetime,timezone
        for item in items:
            values=[]
            for f in fields:
                val=item.get(f)
                if f=='status':
                    val={'new':'Новий','acknowledged':'Підтверджений','working':'У роботі','review':'На перевірці','closed':'Закритий'}.get(val,val)
                if f=='priority':
                    val={'high':'Високий','medium':'Середній','low':'Низький'}.get(val,val)
                if f.endswith('_at') and val is not None:
                    val=datetime.fromtimestamp(val,timezone.utc).isoformat()
                val='' if val is None else str(val)
                if val.startswith(('=','+','-','@','\t','\r')):
                    val="'"+val
                values.append(val)
            writer.writerow(values)
        return Response('\ufeff'+buffer.getvalue(),content_type='text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="{kind}.csv"'})

    if worker:
        app.extensions['simulator_stop']=start_worker(url)
    return app


def seed_demo_without_users(db):
    # seed() has a single transaction and restores demo users only when absent.
    seed(db)


if __name__=='__main__':
    load_env()
    from waitress import serve
    application=create_app(worker=os.environ.get('SIMULATOR_ENABLED','1')=='1')
    host=os.environ.get('HOST','127.0.0.1')
    port=int(os.environ.get('PORT','8000'))
    print(f'IoT DC Monitor: http://{host}:{port}',flush=True)
    serve(application,host=host,port=port,threads=8)
