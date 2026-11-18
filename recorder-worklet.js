// Downmix to mono, decimate to 16 kHz, emit fixed 20 ms frames (320 samples).
const FRAME = 320;

class Recorder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.pos = 0;      // fractional read position into decimation input
    this.acc = 0;
    this.frame = new Float32Array(FRAME);
    this.fill = 0;
    this.stopped = false;
    this.port.onmessage = (event) => {
      if (event.data?.type !== "drain") return;
      this.stopped = true;
      if (this.fill) this.port.postMessage(this.frame.slice(0, this.fill));
      this.fill = 0;
      // 同一消息端口保序：主线程收到此确认时，前面的音频帧已经交付。
      this.port.postMessage({ type: "drained" });
    };
  }

  process(inputs) {
    if (this.stopped) return true;
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const chans = input.length;
    const n = input[0].length;

    for (let i = 0; i < n; i++) {
      // accumulate decimation ratio worth of samples, then take one output sample
      this.acc += 1;
      if (this.acc >= this.ratio) {
        this.acc -= this.ratio;
        let v = 0;
        for (let c = 0; c < chans; c++) v += input[c][i];
        v /= chans;
        this.frame[this.fill++] = v;
        if (this.fill === FRAME) {
          this.port.postMessage(this.frame.slice(0), []);
          this.fill = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("recorder", Recorder);