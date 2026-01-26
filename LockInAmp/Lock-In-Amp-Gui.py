import sys
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from PyQt5.QtWidgets import QApplication, QWidget, QGridLayout, QPushButton, QLineEdit, QSizePolicy, QLabel, QShortcut
from PyQt5.QtCore import Qt, QEvent, QTimer
from PyQt5.QtGui import QFont, QKeySequence, QDoubleValidator
from pyrpl import RedPitaya

# 設定用データクラス
# =========================
@dataclass
class ParamConfig:
    name: str   # データクラスの識別名
    label: str  # 画面に表示する文字列
    initVal: float # パラメータの初期値
    minVal: float  # パラメータの最小値
    maxVal: float  # パラメータの最大値
    normalStep: float  # パラメータの通常モード増加幅
    fineStep: float    # パラメータの微調モード増加幅
    unit: str   # パラメータの単位
    hwUpdateFunc: Optional[Callable[[float], None]] = None    # 値変更時のハードウェア同期用関数
# =========================

class LockInControlPanel(QWidget):
    HOSTNAME = "rp-f047d3.local"

    def __init__(self):
        super().__init__()
        
        # 状態変数
        self.isFineMode = False   # パラメータの増加モードを微調にするか(True:微調/False:通常)
        self.isModOutputEnable = False  # 出力を有効にするか(True:有効/False:無効)
        self.isPidOutputEnable = False  # PID出力を有効にするか(True:有効/False:無効(0Vを出力))
        self.isAutoPhaseEnable = False # 位相自動補正を有効にするか(True:有効/False:無効)
        self.nowSettingParams = {}  # 現在の各設定パラメータを格納する配列
        self.qLineEditWidgets = {}  # テキスト入力ボックスのUI部品を格納する配列

        # 1. ハードウェア初期化の前に、パラメータ定義を作成
        self._setupParamConfigs()
        
        # 2. UI初期化
        self._initUi()
        
        # 3. ハードウェア接続と初期設定
        self._initialize_hardware()
        self._setup_shortcuts()

    def _setupParamConfigs(self):
        """パラメータの定義（名前、ラベル、初期値、範囲、ステップ、単位、更新時の動作）"""
        self.configs = [
            # 変調 (Modulation)
            ParamConfig("mod_freq", "周波数", 1000.0, 0.0, 1000.0, 5.0, 0.1, "Hz", 
                        lambda v: (setattr(self.rp_asg, 'frequency', v), setattr(self.rp_iq, 'frequency', v))),
            ParamConfig("mod_amp", "振幅", 5.0, 0.0, 500.0, 10.0, 1.0, "mV", 
                        lambda v: setattr(self.rp_asg, 'amplitude', v / 1000.0)),
            ParamConfig("mod_off", "オフセット", 0.0, -1000.0, 1000.0, 10.0, 1.0, "mV", 
                        lambda v: setattr(self.rp_asg, 'offset', v / 1000.0)),
            
            # 復調 (Demodulation)
            ParamConfig("demod_gain", "ゲイン", 25.0, 0.001, 1000.0, 10.0, 1.0, "-", 
                        lambda v: setattr(self.rp_iq, 'quadrature_factor', v)),
            ParamConfig("demod_phase", "位相", 0.0, -3600.0, 3600.0, 10.0, 0.1, "deg", 
                        lambda v: setattr(self.rp_iq, 'phase', v % 360)),
            ParamConfig("demod_cutoff", "カットオフ周波数", 100.0, 0.001, 1000.0, 1.0, 0.1, "Hz", 
                        lambda v: setattr(self.rp_iq, 'bandwidth', [v])),

            # PID
            ParamConfig("pid_com", "共通ゲイン", 1.0, -1000.0, 1000.0, 1.0, 0.1, "-", self._update_pid_all),
            ParamConfig("pid_p", "Pゲイン", 0.2, 0.0, 1.0, 0.1, 0.01, "-", self._update_pid_all),
            ParamConfig("pid_i", "Iゲイン", 1.5, 0.0, 1.0, 0.1, 0.01, "-", self._update_pid_all),
            ParamConfig("pid_d", "Dゲイン", 0.0, 0.0, 1.0, 0.1, 0.01, "-", self._update_pid_all),
        ]
        # パラメータ初期値の辞書作成
        for c in self.configs:
            self.nowSettingParams[c.name] = c.initVal

    def _update_pid_all(self, _=None):
        """PIDのゲイン計算（Iゲインは出力無効時に0にする）"""
        if not hasattr(self, 'rp_pid'): return
        
        com = self.nowSettingParams["pid_com"]
        
        # PゲインとDゲインは常にGUIの設定値を反映
        self.rp_pid.p = com * self.nowSettingParams["pid_p"]
        self.rp_pid.d = com * self.nowSettingParams["pid_d"]
        
        # Iゲインの制御：出力有効時のみGUIの設定値を反映。無効時は0.0
        if self.isPidOutputEnable:
            self.rp_pid.i = com * self.nowSettingParams["pid_i"]
        else:
            self.rp_pid.i = 0.0
    
    def _update_monitors(self):
        """タイマーで呼ばれる更新関数"""
        if not hasattr(self, 'rp_pid'): 
            return
        
        try:
            # 診断結果から判明した正確な属性名を使用します
            # 1. PIDの現在の総出力 (current_output_signal)
            v_out = self.rp_pid.current_output_signal
            
            # 2. 積分器の現在の値 (ival)
            v_ival = self.rp_pid.ival
            
            # UI表示の更新
            self.lbl_pid_out.setText(f"{v_out:+.2E}")
            self.lbl_pid_ival.setText(f"{v_ival:+.2E}")
            
            if self.is_auto_phase_enabled:
                self._do_auto_phase()
            
        except Exception as e:
            # 万が一エラーが発生した場合のみ表示
            print(f"Monitor update error: {e}")

    # --- 共通操作ロジック ---
    def _change_value(self, name: str, up: bool):
        """Up/Downボタンが押された時の処理"""
        cfg = next(c for c in self.configs if c.name == name)
        delta = cfg.fineStep if self.isFineMode else cfg.normalStep
        new_val = self.nowSettingParams[name] + (delta if up else -delta)
        
        # 特殊処理：位相のループ
        if name == "demod_phase":
            new_val = new_val % 360
        else:
            new_val = max(cfg.minVal, min(cfg.maxVal, new_val))
            
        self._applyValToHw(name, new_val)
    
    def _reset_integrator(self):
        """積分器の値を0にリセットする"""
        if hasattr(self, 'rp_pid'):
            # ivalレジスタに0を直接代入します
            self.rp_pid.ival = 0
            
            # 即座に表示に反映させる（タイマーを待たずに更新）
            self.lbl_pid_ival.setText(f"{0.0:+.2E}")
            
        print("Integrator Reset: ival = 0")

    def _applyValToHw(self, name: str, val: float):
        """値を内部変数に保存し、ハードウェアとGUIに反映"""
        _cfg = next(c for c in self.configs if c.name == name)   # 設定するデータクラスを取得
        _roundedVal = round(val, 2) # 数値を少数点第2位で丸める
        self.nowSettingParams[name] = _roundedVal   # 設定パラメータを保存
        
        # ハードウェア反映
        if _cfg.hwUpdateFunc:
            _cfg.hwUpdateFunc(_roundedVal)  # ハードウェアに反映する
        
        # GUI反映
        if name in self.qLineEditWidgets:
            self.qLineEditWidgets[name].setText(f"{_roundedVal:.1f}")
        print(f"{_cfg.label} changed to: {_roundedVal} {_cfg.unit}")

    def _processTxtBoxInput(self, name: str):
        """テキストボックスが編集された時の処理"""
        txt = self.qLineEditWidgets[name].text()    # 入力されたテキストを取得
        try:
            val = float(txt)    # Float型に変換する
            self._applyValToHw(name, val)    #
        except ValueError:    # 数値に変換できる文字列でなければ前の数値のままにする
            self.qLineEditWidgets[name].setText(f"{self.nowSettingParams[name]:.1f}")

    # --- UI構築 ---
    def _initUi(self):
        self.setWindowTitle("Lock-In Amp Control Panel")    # ウィンドウタイトルの追加
        self.resize(800, 500)   # 初期ウィンドウサイズの設定
        self.headerFont = QFont(); self.headerFont.setPointSize(10); self.headerFont.setBold(True)   # 共通のフォント設定
        
        layout = QGridLayout()  # 画面のレイアウトを司るインスタンス
        self.setLayout(layout)  # レイアウトをウィンドウに任せる
        layout.setSpacing(10)   # 部品ごとの間隔を設定する
        
        # 部品の幅を設定する
        _colStretch = [3, 3, 3, 3, 3]   # 各部品が置かれている列の幅を入力
        for i, s in enumerate(_colStretch): layout.setColumnStretch(i, s)
        
        # 部品の高さを設定する
        _rowStretch = [1, 1, 1, 2, 2, 1, 1, 1, 2, 2, 1, 1, 1, 1, 1] # 各部品が置かれている行の高さを入力
        for i, s in enumerate(_rowStretch): layout.setRowStretch(i, s)

        # UIセクション構築
        self._addModAndDemodSection(layout, "変調の設定", self.configs[0:3], 0, has_extras=True)    # 変調に関するセクションを追加
        self._addModAndDemodSection(layout, "復調の設定", self.configs[3:6], 5, has_extras=True)    # 復調に関するセクションを追加
        self._addPidSection(layout, "PIDの設定", self.configs[6:], 10)  # PIDに関するセクションを追加

    def _addModAndDemodSection(self, layout, title, configs, start_row, has_extras=False):
        # タイトルラベルを追加
        _titelLabel = QLabel(title); _titelLabel.setFont(self.headerFont); _titelLabel.setAlignment(Qt.AlignCenter)
        layout.addWidget(_titelLabel, start_row, 0, 1, 2)
        
        # TODO (機能追加時)キーを追加するように
        # ショートカットキー割当の設定
        if "変調" in title:
            # タイトルに「変調」という文字が含まれていたら、キーボードの左側のキーを使う
            shortcut_keys = [('W','S'), ('E','D'), ('R','F')]
        else:
            # 含まれていなければ（＝復調のとき）、キーボードの右側のキーを使う
            shortcut_keys = [('T','G'), ('Y','H'), ('U','J')]
        
        # ラベル・テキストボックスの配置
        for i, cfg in enumerate(configs):
            layout.addWidget(QLabel(f"{cfg.label} ({cfg.unit})", font=self.headerFont, alignment=Qt.AlignCenter), start_row+1, i)   # ラベルを配置
            txt = QLineEdit(f"{cfg.initVal:.1f}")   # テキストボックスを配置
            txt.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding) # テキストボックスのサイズの大きさを指定
            txt.setValidator(QDoubleValidator())    # テキストボックスに入力制限を追加する(少数以外は入力できないようにする)
            txt.editingFinished.connect(lambda n=cfg.name: self._processTxtBoxInput(n)) # テキストボックスに値が入力された際の処理の予約
            txt.installEventFilter(self)    # テキストボックス選択時でも，ショートカットキーを認識するためのフィルターをセット
            layout.addWidget(txt, start_row+2, i)   # テキストボックスを配置
            self.qLineEditWidgets[cfg.name] = txt   # テキストボックスの割当を保存
            
            # Up/Downボタン
            _upKey, _downKey = shortcut_keys[i] # 割り当てられたショートカットキーを取得
            btn_up = self._createBtn(f"Up({_upKey.lower()})", lambda _, n=cfg.name: self._change_value(n, True))   # 
            btn_dn = self._createBtn(f"Down({_downKey.lower()})", lambda _, n=cfg.name: self._change_value(n, False))
            layout.addWidget(btn_up, start_row+3, i)
            layout.addWidget(btn_dn, start_row+4, i)

        if has_extras:
            layout.addWidget(QLabel("その他", font=self.headerFont, alignment=Qt.AlignCenter), start_row+1, 4)
            if "変調" in title:
                self.btn_fine = self._createBtn("微調(Shift)", self._toggle_fine_mode, checkable=True)
                self.btn_out = self._createBtn("出力(Space)", self._toggle_mod_output, checkable=True)
                layout.addWidget(self.btn_fine, start_row+3, 4)
                layout.addWidget(self.btn_out, start_row+4, 4)
            else:
                self.btn_auto_phase = self._createBtn("自動補正(P)", self._toggle_auto_phase, checkable=True)
                layout.addWidget(self.btn_auto_phase, start_row+3, 4)

    def _addPidSection(self, layout, title, configs, start_row):
        # --- タイトル (Row 10) ---
        lbl = QLabel(title); lbl.setFont(self.headerFont); lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl, start_row, 0, 1, 5)
        
        # --- PZT用PID設定 (Row 11-12) ---
        # 最初の4つ (pid_com, pid_p, pid_i, pid_d)
        for i, cfg in enumerate(configs[0:4]):
            layout.addWidget(QLabel(f"{cfg.label}", font=self.headerFont, alignment=Qt.AlignCenter), start_row+1, i)
            self.qLineEditWidgets[cfg.name] = self._create_edit(cfg)
            layout.addWidget(self.qLineEditWidgets[cfg.name], start_row+2, i)

        # PID出力ボタン (Row 11-12, 4列目)
        self.btn_pid_out = self._createBtn("PID出力 (X)", self._toggle_pid_output, checkable=True)
        layout.addWidget(self.btn_pid_out, start_row + 1, 4, 2, 1)

        # PZTモニター (Row 13-14)
        monitor_style = "color: #00ff00; background-color: black; font-family: 'Courier New'; font-weight: bold; font-size: 13px; border: 1px solid gray;"
        layout.addWidget(QLabel("PID出力電圧 (V)"), start_row+3, 0)
        self.lbl_pid_out = QLabel("+0.00E+00"); self.lbl_pid_out.setStyleSheet(monitor_style)
        layout.addWidget(self.lbl_pid_out, start_row+4, 0)
        
        layout.addWidget(QLabel("PZT積分器"), start_row+3, 1)
        self.lbl_pid_ival = QLabel("+0.00E+00"); self.lbl_pid_ival.setStyleSheet(monitor_style)
        layout.addWidget(self.lbl_pid_ival, start_row+4, 1)

        # 積分リセット(C)
        self.btn_pid_reset = self._createBtn("積分リセット (C)", self._reset_integrator)
        layout.addWidget(self.btn_pid_reset, start_row + 3, 4, 2, 1)
    
    def _create_edit(self, cfg):
        """QLineEdit作成の共通処理"""
        txt = QLineEdit(f"{cfg.initVal:.1f}")
        txt.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        txt.setValidator(QDoubleValidator())
        txt.editingFinished.connect(lambda n=cfg.name: self._processTxtBoxInput(n))
        txt.installEventFilter(self)
        return txt

    def _createBtn(self, text, callback, checkable=False):
        btn = QPushButton(text)
        btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        btn.setFocusPolicy(Qt.NoFocus)
        if checkable: btn.setCheckable(True)
        btn.clicked.connect(callback)
        return btn

    # --- 特殊トグル処理 ---
    def _toggle_fine_mode(self):
        self.isFineMode = self.btn_fine.isChecked()
        style = "background-color: lightblue; font-weight: bold;" if self.isFineMode else ""
        self.btn_fine.setStyleSheet(style)
        print(f"Fine mode: {'ON' if self.isFineMode else 'OFF'}")

    def _toggle_mod_output(self):
        self.isModOutputEnable = self.btn_out.isChecked()
        if self.isModOutputEnable:
            self.btn_out.setStyleSheet("background-color: #ff4b4b; color: white; font-weight: bold;")
            self.btn_out.setText("出力中 (Space)")
            self.rp_asg.output_direct = "out1"
            # 現在の値を再適用
            for n in ["mod_freq", "mod_amp", "mod_off"]: self._applyValToHw(n, self.nowSettingParams[n])
        else:
            self.btn_out.setStyleSheet("")
            self.btn_out.setText("出力(Space)")
            self.rp_asg.output_direct = "off"
            self.rp_asg.frequency = 0; self.rp_asg.offset = 0; self.rp_asg.amplitude = 0
        print(f"Output: {'ENABLED' if self.isModOutputEnable else 'DISABLED'}")
        
    def _toggle_auto_phase(self):
        """自動補正ボタンの状態を切り替え"""
        self.isAutoPhaseEnable = self.btn_auto_phase.isChecked() 
        
        if self.isAutoPhaseEnable:
            self.btn_auto_phase.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold;")
            self.btn_auto_phase.setText("補正中 (P)")
        else:
            self.btn_auto_phase.setStyleSheet("")
            self.btn_auto_phase.setText("自動補正(P)")

    def _do_auto_phase(self):
        """山登り法による位相の自動最適化（信号の正のピークを維持）"""
        if not hasattr(self, 'rp_iq'): return
        try:
            # 絶対値をとらずに現在の信号値を取得
            # これにより、最も「正に大きい」ポイント（ピーク）を探しに行きます
            v_now = self.rp_iq.current_output_signal
            
            test_step = 1.0
            current_phase = self.nowSettingParams["demod_phase"]
            
            if not hasattr(self, '_prev_v_auto_phase'):
                self._prev_v_auto_phase = v_now
                self._phase_direction = 1.0
                return

            # 前回より値が小さくなったら方向を反転
            # (正の方向に最大化したいので、値が減ったら逆へ行く)
            if v_now < self._prev_v_auto_phase:
                self._phase_direction *= -1.0
            
            # 位相を更新
            new_phase = (current_phase + (self._phase_direction * test_step)) % 360
            self._applyValToHw("demod_phase", new_phase)
            
            self._prev_v_auto_phase = v_now
            
        except Exception as e:
            print(f"Auto Phase Error: {e}")
            
    def _flip_phase(self):
        """位相を180度反転させる（エラー信号の極性を入れ替える）"""
        new_phase = (self.nowSettingParams["demod_phase"] + 180) % 360
        self._applyValToHw("demod_phase", new_phase)
        # 前回の値をリセットして、反転後の位置から再追従させる
        if hasattr(self, '_prev_v_auto_phase'):
            delattr(self, '_prev_v_auto_phase')
        print(f"Phase flipped 180 deg: {new_phase:.1f}")

        
    def _toggle_pid_output(self):
        self.isPidOutputEnable = self.btn_pid_out.isChecked()
        
        # 1. ゲイン設定をハードウェアに再適用
        # これにより、ONにする直前にIゲインが設定値になり、OFFにした瞬間に0になる
        self._update_pid_all()
        
        if self.isPidOutputEnable:
            self.btn_pid_out.setStyleSheet("background-color: #ff4b4b; color: white; font-weight: bold;")
            self.btn_pid_out.setText("PID出力中 (X)")
            if hasattr(self, 'rp_pid'):
                self.rp_pid.output_direct = 'out2'
        else:
            self.btn_pid_out.setStyleSheet("")
            self.btn_pid_out.setText("PID出力 (X)")
            if hasattr(self, 'rp_pid'):
                self.rp_pid.output_direct = 'off'
        
        print(f"PID Output: {'ENABLED' if self.isPidOutputEnable else 'DISABLED'} (I-Gain Sync Done)")

    def _update_monitors(self):
        """タイマーで呼ばれる更新関数"""
        if not hasattr(self, 'rp_pid'): 
            return
        
        try:
            v_out = self.rp_pid.current_output_signal
            v_ival = self.rp_pid.ival
            
            self.lbl_pid_out.setText(f"{v_out:+.2E}")
            self.lbl_pid_ival.setText(f"{v_ival:+.2E}")
            
            # ここが self.isAutoPhaseEnable になっていることを確認
            if self.isAutoPhaseEnable:
                self._do_auto_phase()
            
        except Exception as e:
            print(f"Monitor update error: {e}")

    # --- ハードウェア・システム系 ---
    def _initialize_hardware(self):
        print(f"Connecting to {self.HOSTNAME}...")
        try:
            self.rp = RedPitaya(hostname=self.HOSTNAME)
            self.rp_asg = self.rp.asg1
            self.rp_iq = self.rp.iq0
            self.rp_pid = self.rp.pid1
            
            self.rp_asg.setup(waveform='sin', frequency=0, amplitude=0, offset=0, output_direct='off', trigger_source='immediately')
            self.rp_iq.setup(frequency=self.nowSettingParams["mod_freq"], bandwidth=[self.nowSettingParams["demod_cutoff"]], gain=0, 
                             phase=self.nowSettingParams["demod_phase"], input='in1', output_direct='off', 
                             output_signal='quadrature', acbandwidth=0, quadrature_factor=self.nowSettingParams["demod_gain"])
            self.rp_pid.setup(input='iq0', output_direct='off', p=self.nowSettingParams["pid_p"], i=self.nowSettingParams["pid_i"], 
                              d=self.nowSettingParams["pid_d"], ival=0, inputfilter=[])
            print("Connected & Initialized.")
            
            # モニター更新用タイマーの設定 (100msごとに更新)
            self.monitor_timer = QTimer()
            self.monitor_timer.timeout.connect(self._update_monitors)
            self.monitor_timer.start(100) 
            print("Monitor Timer Started.")
        except Exception as e:
            print(f"Hardware initialization failed: {e}")

    def _setup_shortcuts(self):
        keys = {
            "W": ("mod_freq", True), "S": ("mod_freq", False),
            "E": ("mod_amp", True), "D": ("mod_amp", False),
            "R": ("mod_off", True), "F": ("mod_off", False),
            "T": ("demod_gain", True), "G": ("demod_gain", False),
            "Y": ("demod_phase", True), "H": ("demod_phase", False),
            "U": ("demod_cutoff", True), "J": ("demod_cutoff", False),
        }
        for key, (name, up) in keys.items():
            sc = QShortcut(QKeySequence(key), self)
            sc.activated.connect(lambda n=name, u=up: self._change_value(n, u))
        
        QShortcut(QKeySequence("Space"), self).activated.connect(self.btn_out.click)
        QShortcut(QKeySequence(Qt.Key_Shift), self).activated.connect(self.btn_fine.click)
        QShortcut(QKeySequence("X"), self).activated.connect(self.btn_pid_out.click)
        QShortcut(QKeySequence("C"), self).activated.connect(self.btn_pid_reset.click)
        QShortcut(QKeySequence("P"), self).activated.connect(self.btn_auto_phase.click)
        QShortcut(QKeySequence("Shift+P"), self).activated.connect(self._flip_phase)

    def eventFilter(self, source, event):
        """テキストボックスフォーカス中のキー横取り (元のコードの挙動を維持)"""
        if event.type() == QEvent.KeyPress and source in self.qLineEditWidgets.values():
            key_map = {Qt.Key_W:("mod_freq",True), Qt.Key_S:("mod_freq",False), Qt.Key_E:("mod_amp",True), 
                       Qt.Key_D:("mod_amp",False), Qt.Key_R:("mod_off",True), Qt.Key_F:("mod_off",False),
                       Qt.Key_T:("demod_gain",True), Qt.Key_G:("demod_gain",False), Qt.Key_Y:("demod_phase",True), 
                       Qt.Key_H:("demod_phase",False), Qt.Key_U:("demod_cutoff",True), Qt.Key_J:("demod_cutoff",False)}
            if event.key() in key_map:
                n, u = key_map[event.key()]; self._change_value(n, u); return True
            if event.key() == Qt.Key_Space: self.btn_out.click(); return True
            if event.key() == Qt.Key_Shift: self.btn_fine.click(); return True
            if event.key() == Qt.Key_X: self.btn_pid_out.click(); return True
            if event.key() == Qt.Key_C: self.btn_pid_reset.click(); return True
            
            modifiers = QApplication.keyboardModifiers()
            if event.key() == Qt.Key_P:
                if modifiers & Qt.ShiftModifier:
                    self._flip_phase()
                else:
                    self.btn_auto_phase.click()
                return True
        return super().eventFilter(source, event)

    def closeEvent(self, event):
        print("Hardware Shutting down...")
        self.rp_asg.output_direct = 'off'; self.rp_asg.offset = 0; self.rp_asg.amplitude = 0
        self.rp_pid.output_direct = 'off'; self.rp_iq.input = 'off'
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    panel = LockInControlPanel()
    panel.show()
    sys.exit(app.exec_())