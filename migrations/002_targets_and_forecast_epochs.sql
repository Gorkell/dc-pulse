CREATE TABLE simulation_targets (
    id INTEGER PRIMARY KEY CHECK(id=1),
    temperature_sensor_id TEXT REFERENCES sensors(id) ON DELETE RESTRICT,
    power_sensor_id TEXT REFERENCES sensors(id) ON DELETE RESTRICT
);
INSERT INTO simulation_targets (id,temperature_sensor_id,power_sensor_id)
SELECT 1,
    (SELECT s.id FROM sensors s JOIN equipment e ON e.id=s.equipment_id WHERE e.kind='rack' AND s.metric='temperature' ORDER BY CASE WHEN e.name='R-01' THEN 0 ELSE 1 END,e.name,s.id LIMIT 1),
    (SELECT s.id FROM sensors s JOIN equipment e ON e.id=s.equipment_id WHERE e.kind='ups' AND s.metric='charge' ORDER BY CASE WHEN e.name='UPS-01' THEN 0 ELSE 1 END,e.name,s.id LIMIT 1);
ALTER TABLE sensors ADD COLUMN activation_epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sensors ADD COLUMN analysis_since DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE forecasts ADD COLUMN sensor_epoch INTEGER NOT NULL DEFAULT 0;
INSERT INTO schema_migrations(version) VALUES (2);
