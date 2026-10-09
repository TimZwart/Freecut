import subprocess, numpy as np, imageio_ffmpeg as i, re, sys
ff = i.get_ffmpeg_exe(); f = sys.argv[1]
err = subprocess.run([ff,'-hide_banner','-i',f],capture_output=True,text=True).stderr
print([l.strip() for l in err.splitlines() if 'Stream' in l or 'Duration' in l])
v = subprocess.run([ff,'-v','error','-i',f,'-map','0:v','-vf','scale=16:9,format=gray','-f','rawvideo','-'],capture_output=True).stdout
fr = np.frombuffer(v,np.uint8).reshape(-1,144).mean(1) > 128
a = subprocess.run([ff,'-v','error','-i',f,'-map','0:a:0','-ac','1','-ar','48000','-f','s16le','-'],capture_output=True).stdout
a = np.frombuffer(a,np.int16).astype(float)
fps = 30000/1001; n = len(fr)
print('video frames', n, 'dur', n/fps, 'audio dur', len(a)/48000)
# audio loud per frame
per = np.array([np.sqrt(np.mean(a[int(k/fps*48000):int((k+1)/fps*48000)]**2)) for k in range(n)]) > 3000
# transitions
vt = np.flatnonzero(np.diff(fr.astype(int))); at = np.flatnonzero(np.diff(per.astype(int)))
print('video transitions', len(vt), 'audio transitions', len(at))
d = np.array([at[np.abs(at-v).argmin()]-v for v in vt]); print('extra audio transitions at frames', sorted(set(at)-set(at[[np.abs(at-v).argmin() for v in vt]])))
print('offset (frames) first/last/max abs:', d[:5], d[-5:], np.abs(d).max())
print('frames where video/audio disagree:', (fr!=per).sum())
