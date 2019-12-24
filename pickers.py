import numpy as np

class Trad_PS(object):

  """ Traditional P&S Picker: Z STA/LTA for P; PCA + EN STA/LTA for S
  Algorithm
    trigger picker: Z chn STA/LTA reach thres
    --> pick P: find within p_win
    --> pick S: PCA filter & find winthin s_win

  Inputs
    stream: obspy.stream obj (3 chn, [e, n, z])
    pick_win: win len for STA/LTA ([lwin, swin])
    trig_thres: threshold to trig picker
    pick_thres: threshold for picking (0 to 1.)
    p_win: win len for pick detected P
    s_win: win len for S arrivla searching
    pca_win: time win for calc pca filter
    pca_rng: time range for pca filter
    fd_trhes: minimum value of dominant frequency
    amp_win: time win to get S amplitude
    det_gap: time gap between detections
    freq_band: frequency band for phase picking
    *all time related params are in sec
  Outputs
    all picks in the stream, and header info

  Usage
    import pickers
    picker = pickers.Trad_PS()
    picks = picker.pick()
  """

  def __init__(self, 
               pick_win   = [10., 1.],
               trig_thres = 15.,
               pick_thres = 0.96,
               p_win      = [1., 1.],
               s_win      = [0., 20.],
               pca_win    = 1.,
               pca_rng    = [0., 2.5],
               fd_thres   = 2.5,
               amp_win    = 5.,
               det_gap    = 5.,
               freq_band  = ['highpass',1.],
               samp_rate  = 100.):

    # change sec to points for time params
    self.samp_rate  = samp_rate
    self.pick_win   = [int(self.samp_rate * pick_win[0]), 
                       int(self.samp_rate * pick_win[1])] 
    self.idx_shift  = self.pick_win
    self.trig_thres = trig_thres
    self.pick_thres = pick_thres
    self.p_win      = [int(self.samp_rate * p_win[0]), 
                       int(self.samp_rate * p_win[1])]
    self.s_win      = [int(self.samp_rate * s_win[0]), 
                       int(self.samp_rate * s_win[1])]
    self.pca_win    =  int(self.samp_rate * pca_win)
    self.pca_rng    = [int(self.samp_rate * pca_rng[0]),
                       int(self.samp_rate * pca_rng[1])]
    self.fd_thres   = fd_thres
    self.amp_win    = int(self.samp_rate * amp_win)
    self.det_gap    = int(self.samp_rate * det_gap)
    self.freq_band  = freq_band


  def pick(self, stream, out_file=None):

    # set output format
    dtype = [('net','O'),
             ('sta','O'),
             ('sta_ot','O'),
             ('p_arr','O'),
             ('s_arr','O'),
             ('s_amp','O'),
             ('p_snr','O'),
             ('s_snr','O'),
             ('freq_dmnt','O')]

    # time alignment
    start_time = max([trace.stats.starttime for trace in stream])
    end_time   = min([trace.stats.endtime for trace in stream])
    if start_time > end_time: return np.array([], dtype=dtype)
    stream = stream.slice(start_time, end_time)
    if len(stream)!=3: return np.array([], dtype=dtype)

    # get header
    head = stream[0].stats
    net  = head.network
    sta  = head.station

    # preprocess & extract data
    stream.detrend('demean').detrend('linear').taper(max_percentage=0.05, max_length=10.)
    flt_type = self.freq_band[0]
    freqmin  = self.freq_band[1]
    if len(self.freq_band)==2:
        stream.filter(flt_type, freq=freqmin)
    elif len(self.freq_band)==3: 
        freqmax = self.freq_band[2]
        stream.filter(flt_type, freqmin=freqmin, freqmax=freqmax)
    data = np.array([trace.data for trace in stream])

    # pick P and S
    picks = []
    print('-'*40)
    # 1. trig picker
    print('1. triggering phase picker: {}.{}, {}'.format(net,sta, start_time))
    cf_trig = self.calc_cf(data[2], self.pick_win)
    trig_ppk = np.where(cf_trig > self.trig_thres)[0]
    slide_idx = 0
    print('2. picking phase:')
    for _ in trig_ppk:

        # 2. pick P around idx_trig
        idx_trig = trig_ppk[slide_idx]
        if idx_trig < self.p_win[0] + self.idx_shift[0]: 
            slide_idx += 1; continue
        data_p = data[2][idx_trig - self.p_win[0] - self.idx_shift[0]
                        :idx_trig + self.p_win[1] + self.idx_shift[1]]
        cf_p = self.calc_cf(data_p, self.pick_win)
        idx_p = idx_trig - self.idx_shift[0] - self.p_win[0] +\
                np.where(cf_p >= self.pick_thres * np.amax(cf_p))[0][0]
        tp = start_time + idx_p / self.samp_rate

        # 3. pick S after P
        # calc cf on E&N
        if len(data[0]) < idx_p + self.s_win[1]: break
        s_rng = [idx_p - self.s_win[0] - self.idx_shift[0],
                 idx_p + self.s_win[1] + self.idx_shift[1]]
        data_s = np.sqrt(data[0][s_rng[0] : s_rng[1]]**2\
                       + data[1][s_rng[0] : s_rng[1]]**2)
        cf_s = self.calc_cf(data_s, self.pick_win)

        # trig S picker and pick
        pca_flt = self.calc_filter(data, idx_p)
        data_s[self.idx_shift[0] : self.idx_shift[0] + len(pca_flt)] *= pca_flt
        s_trig = np.argmax(data_s[self.idx_shift[0]:]) + self.idx_shift[0]
        s_rng0 = min(s_trig, int(s_trig + self.idx_shift[0])//2)
        s_rng1 = max(s_trig, int(s_trig + self.idx_shift[0])//2)
        if s_rng0==s_rng1: s_rng1+=1
        cf_s = cf_s[s_rng0 : s_rng1]
        idx_s = idx_p - self.idx_shift[0] - self.s_win[0]\
              + s_rng0 + np.argmax(cf_s)
        ts = start_time + idx_s / self.samp_rate

        # get related S amplitude
        amp_xyz = np.array([self.get_amp(di) for di in data[:, idx_s : idx_s + self.amp_win]])
        amp = np.sqrt(np.sum(amp_xyz**2))

        # get p_anr and s_anr
        p_snr = np.amax(cf_p)
        s_snr = np.amax(cf_s)

        # calc dominant frequency
        t0 = min(tp, ts)
        t1 = min(tp+(ts-tp)/2, head.endtime)
        st = stream.slice(t0,t1)
        fd = max([self.calc_freq_dmnt(tr.data, 1/self.samp_rate) for tr in st])

        # output
        print('{}, {}, {}'.format(sta, tp, ts))
        if tp<ts and fd>self.fd_thres:
            ot0 = self.est_ot(tp, ts) # est. of ot for assoc
            picks.append((net, sta, ot0, tp, ts, amp, p_snr, s_snr, fd))
            if out_file: 
                pick_line = '{},{},{},{},{},{},{:.2f},{:.2f},{:.2f}\n'\
                              .format(net, sta, ot0, tp, ts, amp, p_snr, s_snr, fd)
                out_file.write(pick_line)

        # next detected phase
        rest_det = np.where(trig_ppk >\
                   max(idx_trig, idx_s, idx_p) + self.det_gap)[0]
        if len(rest_det)==0: break
        slide_idx = rest_det[0]

    # convert to structed np.array
    return np.array(picks, dtype=dtype)


  def calc_cf(self, data, win_len):
    """  calc character func of STA/LTA for a single trace
    Inputs
        data: input trace data, in np.array
        win_len: win len for STA/LTA, [lwin, swin], in points
    Outputs
        cf: character function
    """
    lwin, swin = [int(win) for win in win_len]
    if len(data)<lwin+swin:
        print('input data too short!')
        return np.zeros(1)
    sta = np.zeros(len(data))
    lta = np.ones(len(data))
    # use energy
    data = np.cumsum(data**2)
    # Compute the STA and the LTA
    sta[:-swin] = data[swin:] - data[:-swin]
    sta /= swin
    lta[lwin:]  = data[lwin:] - data[:-lwin]
    lta /= lwin
    # Pad zeros (same out size as data)
    sta[:lwin] = 0
    cf = sta/lta
    # avoid bad points
    cf[np.isinf(cf)] = 0.
    cf[np.isnan(cf)] = 0.
    return cf


  def calc_filter(self, data, idx_p):
    """ calc S filter by PCA
    Inputs:
        data (np.array): input 3-chn data
        idx_p (data points): idx for P in data
    Outputs:
        pca_flt (np.array): pca filter for P wave filtering
    """
    p_mat = data[:, idx_p : idx_p + self.pca_win]
    p_r, p_evec = self.calc_pol(p_mat)
    # calc filter
    idx_rng = range(idx_p - self.s_win[0] - self.pca_rng[0],
                    idx_p - self.s_win[0] + self.pca_rng[1])
    pca_flt = np.zeros(len(idx_rng))
    for i, idx in enumerate(idx_rng):
        s_mat = data[:, idx : idx + self.pca_win]
        s_r, s_evec = self.calc_pol(s_mat)
        u11 = abs(np.dot(p_evec, s_evec))
        pca_flt[i] = s_r * (1-u11)
    return pca_flt


  def calc_pol(self, mat):
    """ calc polarization by PCA
    Inputs
        mat: 3-chn time win (matrix)
    Outputs
        r: polirization degree
        vec: dominant eig-vector
    """
    cov = np.cov(mat)
    e_val, e_vec = np.linalg.eig(cov)
    # calc pol degree
    lam1  = np.amax(e_val)
    lam23 = np.sum(e_val) - lam1
    r = 1 - (0.5 * lam23 / lam1)
    # calc dom vec
    vec = e_vec.T[np.argmax(e_val)]
    return r, vec


  # estimate original time
  def est_ot(self, tp, ts):
    vp, vs = 5.9, 3.4
    d = (ts-tp) /(1/vs - 1/vp)
    tt_p = d / vp
    return tp - tt_p


  # get S amplitide
  def get_amp(self, velo):
    # velocity to displacement
    disp = np.zeros(len(velo))
    for i in range(len(velo)-1):
        disp[i+1] = np.sum(velo[0:i])
    disp = disp / self.samp_rate
    return (np.amax(disp) - np.amin(disp)) /2


  # calc dominant frequency
  def calc_freq_dmnt(self, data, dt):
    npts = len(data)
    if npts//2==0: return 0
    data = data - np.mean(data)
    psd = abs(np.fft.fft(data))**2
    psd = psd[:npts//2]
    return np.argmax(psd) /dt/npts

