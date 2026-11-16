"""程序合成的 60 秒轻音乐垫底：柔和合成垫 + 稀疏的八音盒式琶音。纯标准库，输出 16bit 单声道 WAV。"""
import math, random, struct, wave, sys

SR = 44100
DUR = 61.0
BPM = 72
BEAT = 60 / BPM
random.seed(11)

def midi(n): return 440.0 * 2 ** ((n - 69) / 12)

# 四个和弦各 2 小节（8 拍），循环：Cmaj9 / Am9 / Fmaj9 / G add9
CHORDS = [
    [48, 55, 59, 62, 64],   # C  G  B  D  E
    [45, 52, 55, 59, 60],   # A  E  G  B  C
    [41, 48, 52, 55, 57],   # F  C  E  G  A
    [43, 50, 54, 55, 57],   # G  D  F# G  A
]
PENTA = [[60, 62, 64, 67, 69, 72, 74, 76], [57, 60, 62, 64, 67, 69, 72, 74],
         [60, 62, 64, 65, 67, 69, 72, 76], [59, 62, 64, 67, 69, 71, 74, 76]]
CHORD_LEN = 8 * BEAT  # 6.67 s

N = int(SR * DUR)
bufL = [0.0] * N
bufR = [0.0] * N

def add_pad(start, length, notes):
    a, r = 2.2, 2.6
    s0 = int(start * SR); n = int((length + r) * SR)
    for m in notes:
        f = midi(m); ph = random.random() * 6.283
        det = 1 + random.uniform(-0.0012, 0.0012)
        for i in range(n):
            t = i / SR
            env = min(1.0, t / a) * (1.0 if t < length else max(0.0, 1 - (t - length) / r))
            if env <= 0: continue
            # 基音 + 弱二次谐波 + 慢速颤音，模拟柔和的弦乐/合成垫
            x = math.sin(6.283 * f * det * t + ph) + 0.28 * math.sin(6.283 * 2 * f * t + ph) * (1 + 0.1 * math.sin(6.283 * 0.3 * t))
            j = s0 + i
            if j < N:
                bufL[j] += 0.040 * env * x
                bufR[j] += 0.040 * env * math.sin(6.283 * f * (2 - det) * t + ph + 0.4)

def add_pluck(start, m, vel):
    f = midi(m); n = int(2.4 * SR); s0 = int(start * SR)
    pan = max(-0.45, min(0.45, (m - 66) / 22))  # 低音偏左、高音偏右
    gl = math.sqrt(0.5 - pan / 2) * 1.25; gr = math.sqrt(0.5 + pan / 2) * 1.25
    for i in range(n):
        t = i / SR
        env = math.exp(-t * 2.3) * (1 - math.exp(-t * 400))
        x = math.sin(6.283 * f * t) + 0.45 * math.exp(-t * 4) * math.sin(6.283 * 2 * f * t) + 0.18 * math.exp(-t * 7) * math.sin(6.283 * 3 * f * t)
        j = s0 + i
        if j < N:
            bufL[j] += vel * env * x * gl
            bufR[j] += vel * env * x * gr

# 和弦垫
t = 0.0; k = 0
while t < DUR:
    add_pad(t, CHORD_LEN, CHORDS[k % 4]); t += CHORD_LEN; k += 1

# 琶音：每半拍一个落点，约 45% 概率发声，音高在当前和弦的五声音阶里缓慢游走
t = 0.0; idx = 3; last = None
while t < DUR - 2:
    k = int(t // CHORD_LEN) % 4
    scale = PENTA[k]
    if random.random() < (0.6 if (t % CHORD_LEN) < BEAT * 0.3 else 0.5):
        idx = max(0, min(len(scale) - 1, idx + random.choice([-2, -1, -1, 0, 1, 1, 2])))
        m = scale[idx]
        if m != last:
            add_pluck(t + random.uniform(-0.01, 0.01), m, random.uniform(0.11, 0.17))
            last = m
    t += BEAT / 2

# 整体淡入淡出 + 归一化
peak = max(max(abs(v) for v in bufL), max(abs(v) for v in bufR)) or 1
out = wave.open(sys.argv[1], 'wb'); out.setnchannels(2); out.setsampwidth(2); out.setframerate(SR)
frames = bytearray()
for i in range(N):
    tt = i / SR
    g = min(1.0, tt / 2.0) * min(1.0, max(0.0, (DUR - tt) / 3.0))
    frames += struct.pack('<hh', int(max(-1, min(1, bufL[i] / peak * 0.9 * g)) * 32767), int(max(-1, min(1, bufR[i] / peak * 0.9 * g)) * 32767))
out.writeframes(bytes(frames)); out.close()
print('ok', sys.argv[1])
