import sys, time, tkinter as tk
sys.path.insert(0, '.')
from PIL import ImageGrab
from quietcut import app as A, player as P
import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
orig = P.Player._callback
def muted(self, outdata, *a):
    orig(self, outdata, *a); outdata[:] = 0
P.Player._callback = muted
root = tk.Tk(); a = A.App(root)
out, media = sys.argv[1], sys.argv[2]
def shot(name):
    root.update(); x, y = root.winfo_rootx(), root.winfo_rooty()
    ImageGrab.grab((x, y, x+root.winfo_width(), y+root.winfo_height()), all_screens=True).save(f'{out}/{name}.png')
def step():
    a.open_file(media)
    def wait():
        if a.busy or a.an is None: return root.after(100, wait)
        a.vars['threshold_db'].set(-40); a.recompute()
        root.after(700, loaded)
    wait()
def loaded():
    print('tracks', [s.label for s in a.info.audio], 'cuts all-tracks:', len(a.cuts))
    shot('1_loaded')
    # click the header of lane 2
    ids = a.canvas.find_withtag('lane1'); x0,y0,x1,y1 = a.canvas.bbox(ids[0])
    a.canvas.event_generate('<Motion>', x=(x0+x1)//2, y=(y0+y1)//2); root.update()
    a.canvas.event_generate('<ButtonPress-1>', x=(x0+x1)//2, y=(y0+y1)//2); a.canvas.event_generate('<ButtonRelease-1>', x=(x0+x1)//2, y=(y0+y1)//2)
    print('track_on', a.track_on, 'cuts mic-only:', len(a.cuts))
    a._set_view(8, 14); a._seek(9.0); root.after(600, lambda: (shot('2_mic_only'), play()))
def play():
    a.start_play(); t0 = time.time(); ph0 = a.playhead
    def check():
        shot('3_playing')
        p = a.player
        print('playing', p is not None, 'played %.2fs audio' % (p.played/48000), 'playhead %.2f -> %.2f' % (ph0, a.playhead),
              'photo', a.photo_size, 'result-time', round(a._src_to_out(a.playhead)-a._src_to_out(ph0),2), 'wall', round(time.time()-t0,2))
        a.stop_play(); a._on_close()
    root.after(3000, check)
root.after(400, step); root.mainloop()
