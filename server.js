const express=require("express");
const multer=require("multer");
const fs=require("fs");
const path=require("path");
const {spawn}=require("child_process");
const crypto=require("crypto");
const ffmpeg=require("ffmpeg-static");

const app=express(), PORT=process.env.PORT||3000;
const DATA=path.join(__dirname,"data"), RECORDINGS=path.join(DATA,"recordings"), UPLOADS=path.join(DATA,"uploads"), DB=path.join(DATA,"streamers.json"), RDB=path.join(DATA,"recordings.json");
for(const d of [DATA,RECORDINGS,UPLOADS])fs.mkdirSync(d,{recursive:true});
if(!fs.existsSync(DB))fs.writeFileSync(DB,"[]");
if(!fs.existsSync(RDB))fs.writeFileSync(RDB,"[]");
app.use(express.json({limit:"1mb"}));
app.use(express.static(path.join(__dirname,"public")));
app.use("/media",express.static(RECORDINGS));
const upload=multer({dest:UPLOADS,limits:{fileSize:2*1024*1024*1024}});
const read=p=>JSON.parse(fs.readFileSync(p,"utf8")), write=(p,v)=>fs.writeFileSync(p,JSON.stringify(v,null,2));
let recordings=read(RDB);
const id=()=>crypto.randomUUID();
const authorized=s=>s&&s.authorized===true&&(!s.expiresAt||new Date(s.expiresAt)>new Date());

app.get("/api/streamers",(_,res)=>res.json(read(DB)));
app.post("/api/streamers",(req,res)=>{
 const {name,platform,profileUrl,sourceUrl,permissionNote,permissionDate,expiresAt}=req.body;
 if(!name||!platform||!sourceUrl||!permissionDate)return res.status(400).json({error:"name, platform, sourceUrl and permissionDate are required"});
 const db=read(DB), s={id:id(),name,platform,profileUrl:profileUrl||"",sourceUrl,permissionNote:permissionNote||"",permissionDate,expiresAt:expiresAt||null,authorized:true,createdAt:new Date().toISOString()};
 db.push(s);write(DB,db);res.json(s);
});
app.patch("/api/streamers/:id",(req,res)=>{const db=read(DB),i=db.findIndex(x=>x.id===req.params.id);if(i<0)return res.status(404).json({error:"Not found"});db[i]={...db[i],...req.body};write(DB,db);res.json(db[i])});
app.delete("/api/streamers/:id",(req,res)=>{const db=read(DB),i=db.findIndex(x=>x.id===req.params.id);if(i<0)return res.status(404).json({error:"Not found"});db.splice(i,1);write(DB,db);res.json({ok:true})});

app.post("/api/record",(req,res)=>{
 const {streamerId,duration=3600}=req.body, s=read(DB).find(x=>x.id===streamerId);
 if(!authorized(s))return res.status(403).json({error:"Streamer is not currently authorized."});
 const seconds=Math.max(1,Math.min(Number(duration)||3600,12*3600)), rid=id(), output=path.join(RECORDINGS,rid+".mp4");
 const job={id:rid,streamerId,streamerName:s.name,startedAt:new Date().toISOString(),status:"recording",file:"/media/"+rid+".mp4",duration:seconds};
 const ff=spawn(ffmpeg,["-hide_banner","-loglevel","warning","-i",s.sourceUrl,"-t",String(seconds),"-c","copy","-movflags","+faststart",output]);
 job.pid=ff.pid;recordings.unshift(job);write(RDB,recordings);
 ff.on("close",code=>{job.finishedAt=new Date().toISOString();job.status=code===0?"complete":"failed";delete job.pid;write(RDB,recordings)});
 ff.on("error",e=>{job.status="failed";job.error=e.message;delete job.pid;write(RDB,recordings)});
 res.json(job);
});
app.get("/api/recordings",(_,res)=>res.json(recordings));
app.post("/api/recordings/:id/stop",(req,res)=>{const r=recordings.find(x=>x.id===req.params.id);if(!r||!r.pid)return res.status(404).json({error:"Active recording not found"});try{process.kill(r.pid,"SIGINT")}catch{}res.json({ok:true})});
app.post("/api/upload",upload.single("video"),(req,res)=>{if(!req.file)return res.status(400).json({error:"No video supplied"});const n=id()+"-"+path.basename(req.file.originalname).replace(/[^a-zA-Z0-9._-]/g,"_");const p=path.join(RECORDINGS,n);fs.renameSync(req.file.path,p);res.json({ok:true,file:"/media/"+n})});
app.get("*",(_,res)=>res.sendFile(path.join(__dirname,"public","index.html")));
app.listen(PORT,"0.0.0.0",()=>console.log("Authorized Stream Recorder on port "+PORT));