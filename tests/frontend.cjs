// Test the actual chart/overview functions without a browser or external packages.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('static/app.js','utf8').split('setInterval(async()=>')[0];
const nodes=new Map();
const context=vm.createContext({document:{querySelector(selector){if(!nodes.has(selector))nodes.set(selector,{innerHTML:''});return nodes.get(selector)}},setTimeout,clearTimeout,console});
vm.runInContext(source,context);
for(const [metric,lo,hi,unit] of [['temperature',18,32,'°C'],['humidity',25,65,'%'],['charge',30,100,'%'],['voltage',210,245,'В']]){
  context.fixture={points:[{measured_at:100,value:hi-1},{measured_at:200,value:lo-1}],sensor:{name:metric,unit,lower_bound:lo,upper_bound:hi}};
  const svg=vm.runInContext('chart(fixture.points,fixture.sensor)',context);
  assert(svg.includes(`Нижній поріг ${lo} ${unit}`));
  assert(svg.includes(`Верхній поріг ${hi} ${unit}`));
  assert.equal((svg.match(/data-bound=/g)||[]).length,2);
  const lowerY=Number(svg.match(/data-bound="lower"[^>]* y1="([\d.]+)"/)[1]);
  const lastY=Number(svg.match(/class="actual" d="M[^L]+L[\d.]+,([\d.]+)"/)[1]);
  assert(lastY>lowerY,`${metric}: actual line must cross below lower threshold`);
  assert(lowerY<210&&lowerY>30,'Both thresholds fit inside chart bounds');
}
(async()=>{
  context.fixtureState={sensors:[],equipment:[],incidents:[],notifications:[],simulation:{clock:1000,speed:1,scenario:'normal',running:0},targets:{temperature_sensor_id:null,temperature_error:'Ціль відсутня'}};
  vm.runInContext("state=fixtureState; user={role:'operator'}",context);
  await vm.runInContext('overview()',context);
  assert(nodes.get('#content').innerHTML.includes('Ціль відсутня'));
  context.fixtureState.sensors=[{id:'stable',equipment_id:'rack',state:'inactive',active:0,metric:'temperature'}];
  context.fixtureState.equipment=[{id:'rack',name:'Нова назва',active:1}];
  context.fixtureState.targets={temperature_sensor_id:'stable',temperature_error:'Ціль архівована'};
  await vm.runInContext('overview()',context);
  assert(nodes.get('#content').innerHTML.includes('Температура · Нова назва'));
  assert(nodes.get('#content').innerHTML.includes('Ціль архівована'));
  console.log('Frontend regression checks: 4 metrics, missing/archived overview targets — OK');
})().catch(e=>{console.error(e);process.exitCode=1});
