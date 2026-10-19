/* StudentConnect wow layer: logo tiles, count-up, sparklines, spotlight/tilt, cursor glow, Three.js scenes. Safe to run repeatedly. */
(()=>{
const RM=matchMedia('(prefers-reduced-motion:reduce)').matches,$=(s)=>[...document.querySelectorAll(s)];
const hue=s=>{let h=0;for(const c of s)h=(h*31+c.charCodeAt(0))%360;return h};
function init(){
$('.jc,.kc,tbody td:first-child').forEach(el=>{
 if(el.dataset.t)return;const co=el.querySelector('.co'),role=el.querySelector('.role');if(!co||!role)return;
 const name=co.textContent.split('·')[0].trim();if(!name)return;el.dataset.t=1;
 let p=role.parentNode;if(p===el){p=document.createElement('div');role.before(p);p.append(role,co)}
 const t=document.createElement('span');t.className='logo-tile';t.textContent=name[0];t.setAttribute('aria-hidden','true');
 t.style.background='hsl('+hue(name)+' 62% 40%)';p.classList.add('with-logo');p.prepend(t)});
$('.k .v').forEach(el=>{
 if(el.dataset.c)return;el.dataset.c=1;const m=el.textContent.trim().match(/^(\d+)(.*)$/);if(!m||RM)return;
 const n=+m[1],s=m[2],t0=performance.now()+300;el.textContent='0'+s;
 const f=t=>{const p=Math.min(Math.max((t-t0)/1300,0),1);el.textContent=Math.round(n*(1-Math.pow(2,-10*p)))+s;if(p<1)requestAnimationFrame(f)};requestAnimationFrame(f)});
$('[data-spark]').forEach(c=>{
 if(c.dataset.s)return;c.dataset.s=1;const a=c.dataset.spark.split(',').map(Number),lo=Math.min(...a),hi=Math.max(...a)||1;
 const d=a.map((v,i)=>(i?'L':'M')+(i*100/(a.length-1)).toFixed(1)+' '+(28-(v-lo)/((hi-lo)||1)*26).toFixed(1)).join('');
 c.insertAdjacentHTML('beforeend','<svg class="spark" viewBox="0 0 100 30" aria-hidden="true"><path pathLength="1" d="'+d+'"/></svg>')});
$('.card,.kc,.jc').forEach(el=>{
 if(el.dataset.h)return;el.dataset.h=1;const tilt=!RM&&el.matches('.kpis .card,.kc,.jc');
 el.addEventListener('pointermove',e=>{const r=el.getBoundingClientRect(),x=(e.clientX-r.left)/r.width-.5,y=(e.clientY-r.top)/r.height-.5;
  el.style.setProperty('--mx',e.clientX-r.left+'px');el.style.setProperty('--my',e.clientY-r.top+'px');
  if(tilt)el.style.transform='perspective(800px) rotateX('+(-y*7)+'deg) rotateY('+(x*9)+'deg) translateY(-3px)'});
 el.addEventListener('pointerleave',()=>{el.style.transform=''})});
$('.btn.p').forEach(b=>{
 if(b.dataset.m||RM)return;b.dataset.m=1;
 b.addEventListener('pointermove',e=>{const r=b.getBoundingClientRect();b.style.transform='translate('+(e.clientX-r.left-r.width/2)*.12+'px,'+(e.clientY-r.top-r.height/2)*.18+'px)'});
 b.addEventListener('pointerleave',()=>{b.style.transform=''})});
if(!RM&&!document.querySelector('.cursor-glow')){
 const g=document.createElement('div');g.className='cursor-glow';document.body.append(g);
 addEventListener('pointermove',e=>{g.style.transform='translate('+e.clientX+'px,'+e.clientY+'px)';g.classList.add('on')})}
$('.auth-hero,.hero-banner[data-scene]').forEach(scene)}
function scene(host){
 if(host.dataset.3||!window.THREE||RM||matchMedia('(max-width:760px)').matches)return;host.dataset['3']=1;
 const T=THREE,cv=document.createElement('canvas');cv.className='scene';cv.setAttribute('aria-hidden','true');host.prepend(cv);
 const R=new T.WebGLRenderer({canvas:cv,antialias:true,alpha:true});R.setPixelRatio(Math.min(devicePixelRatio,2));
 const S=new T.Scene(),C=new T.PerspectiveCamera(50,1,.1,100),G=new T.Group();C.position.z=11;S.add(G);
 if(host.dataset.scene==='right')G.position.x=4.2;
 const P=[];for(let i=0;i<110;i++){const u=Math.random()*2-1,a=Math.random()*6.283,r=3+Math.random()*3.5,s=Math.sqrt(1-u*u);P.push(new T.Vector3(r*s*Math.cos(a),r*u*.8,r*s*Math.sin(a)))}
 const pts=(v,c,z)=>new T.Points(new T.BufferGeometry().setFromPoints(v),new T.PointsMaterial({color:c,size:z,transparent:true,opacity:.9}));
 G.add(pts(P,0x9db0ff,.11),pts(P.filter((_,i)=>i%14===0),0xff7a59,.24));
 const L=[];for(let i=0;i<P.length;i++)for(let j=i+1;j<P.length;j++)if(P[i].distanceTo(P[j])<2.1)L.push(P[i],P[j]);
 G.add(new T.LineSegments(new T.BufferGeometry().setFromPoints(L),new T.LineBasicMaterial({color:0x4f6bff,transparent:true,opacity:.3})));
 const wf=(g,c,o)=>new T.Mesh(g,new T.MeshBasicMaterial({color:c,wireframe:true,transparent:true,opacity:o})),
 core=wf(new T.IcosahedronGeometry(1.5,1),0xff7a59,.85),halo=wf(new T.IcosahedronGeometry(2.3,0),0x8fa3ff,.25);G.add(core,halo);
 const fit=()=>{const w=host.clientWidth,h=host.clientHeight;if(!w||!h)return;R.setSize(w,h,false);C.aspect=w/h;C.updateProjectionMatrix()};
 new ResizeObserver(fit).observe(host);fit();
 let mx=0,my=0,run=true;host.addEventListener('pointermove',e=>{const r=host.getBoundingClientRect();mx=(e.clientX-r.left)/r.width-.5;my=(e.clientY-r.top)/r.height-.5});
 document.addEventListener('visibilitychange',()=>{run=!document.hidden;if(run)loop(performance.now())});
 const loop=t=>{if(!run)return;
  G.rotation.y=t*.00007+mx*.6;G.rotation.x=my*.3;core.rotation.x+=.004;core.rotation.y+=.006;halo.rotation.y-=.003;core.scale.setScalar(1+Math.sin(t*.002)*.07);
  C.position.x+=(mx*2-C.position.x)*.05;C.position.y+=(-my*1.5-C.position.y)*.05;C.lookAt(0,0,0);
  if(cv.clientWidth)R.render(S,C);requestAnimationFrame(loop)};requestAnimationFrame(loop)}
const go=()=>{init();setTimeout(init,600);setTimeout(init,1800)};
document.readyState==='loading'?addEventListener('DOMContentLoaded',go):go();
})();
