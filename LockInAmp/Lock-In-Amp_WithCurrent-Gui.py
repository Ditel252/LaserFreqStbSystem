import sys
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from PyQt5.QtWidgets import QApplication, QWidget, QGridLayout, QPushButton, QLineEdit, QSizePolicy, QLabel, QShortcut
from PyQt5.QtCore import Qt, QEvent, QTimer
from PyQt5.QtGui import QFont, QKeySequence, QDoubleValidator
from pyrpl import RedPitaya

# --- 設定値の集約 ---
@dataclass
class ParamConfig:
    name: str
    label: str
    init_val: float
    min_val: float
    max_val: float
    step_normal: float
    step_fine: float
    unit: str
    hw_update_func: Optional[Callable[[float], None]] = None

class LockInControlPanel(QWidget):
    HOSTNAME = "rp-f047d3.local"

    def __init__(self):
        super().__init__()
        # 状態変数
        self.is_fine_mode = False
        self.is_output_enabled = False
        self.is_pid_enabled = False
        self.params = {}  # 各パラメータの現在値を保持
        self.widgets = {} # 各パラメータのUI要素を保持
        self.is_centering_enabled = False
        self.center_err_sum = 0.0
        self.center_last_err = 0.0

        # 1. ハードウェア初期化の前に、パラメータ定義を作成
        self._setup_param_configs()
        
        # 2. UI初期化
        self._init_ui()
        
        # 3. ハードウェア接続と初期設定
        self._initialize_hardware()
        self._setup_shortcuts()

    def _setup_param_configs(self):
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
            # LD中心引き戻し (Software PID)
            ParamConfig("center_p", "Center-P", 0.0, -100.0, 100.0, 0.1, 0.01, "-", None),
            ParamConfig("center_i", "Center-I", 0.0, -100.0, 100.0, 0.1, 0.01, "-", None),
            ParamConfig("center_d", "Center-D", 0.0, -100.0, 100.0, 0.1, 0.01, "-", None),
        ]
        # パラメータ初期値の辞書作成
        for c in self.configs:
            self.params[c.name] = c.init_val

    def _update_pid_all(self, _=None):
        """PIDのゲイン計算（Iゲインは出力無効時に0にする）"""
        if not hasattr(self, 'rp_pid'): return
        
        com = self.params["pid_com"]
        
        # PゲインとDゲインは常にGUIの設定値を反映
        self.rp_pid.p = com * self.params["pid_p"]
        self.rp_pid.d = com * self.params["pid_d"]
        
        # Iゲインの制御：出力有効時のみGUIの設定値を反映。無効時は0.0
        if self.is_pid_enabled:
            self.rp_pid.i = com * self.params["pid_i"]
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
            
        except Exception as e:
            # 万が一エラーが発生した場合のみ表示
            print(f"Monitor update error: {e}")

    # --- 共通操作ロジック ---
    def _change_value(self, name: str, up: bool):
        """Up/Downボタンが押された時の処理"""
        cfg = next(c for c in self.configs if c.name == name)
        delta = cfg.step_fine if self.is_fine_mode else cfg.step_normal
        new_val = self.params[name] + (delta if up else -delta)
        
        # 特殊処理：位相のループ
        if name == "demod_phase":
            new_val = new_val % 360
        else:
            new_val = max(cfg.min_val, min(cfg.max_val, new_val))
            
        self._apply_value(name, new_val)
    
    def _reset_integrator(self):
        """積分器の値を0にリセットする"""
        if hasattr(self, 'rp_pid'):
            # ivalレジスタに0を直接代入します
            self.rp_pid.ival = 0
            
            # 即座に表示に反映させる（タイマーを待たずに更新）
            self.lbl_pid_ival.setText(f"{0.0:+.2E}")
            
        print("Integrator Reset: ival = 0")

    def _apply_value(self, name: str, val: float):
        """値を内部変数に保存し、ハードウェアとGUIに反映"""
        cfg = next(c for c in self.configs if c.name == name)
        rounded_val = round(val, 2)
        self.params[name] = rounded_val
        
        # ハードウェア反映
        if cfg.hw_update_func:
            cfg.hw_update_func(rounded_val)
        
        # GUI反映
        if name in self.widgets:
            self.widgets[name].setText(f"{rounded_val:.1f}")
        print(f"{cfg.label} changed to: {rounded_val} {cfg.unit}")

    def _on_txt_edited(self, name: str):
        """テキストボックスが編集された時の処理"""
        txt = self.widgets[name].text()
        try:
            val = float(txt)
            self._apply_value(name, val)
        except ValueError:
            self.widgets[name].setText(f"{self.params[name]:.1f}")

    # --- UI構築 ---
    def _init_ui(self):
        self.setWindowTitle("Lock-In Amp Control Panel")
        self.resize(800, 700)
        self.header_font = QFont(); self.header_font.setPointSize(10); self.header_font.setBold(True)
        
        layout = QGridLayout()
        self.setLayout(layout)
        layout.setSpacing(10)
        
        # 列のストレッチ設定を 5列すべて 3 にして均等化する
        col_stretch = [3, 3, 3, 3, 3] # [3, 3, 3, 1, 3] から変更
        for i, s in enumerate(col_stretch): layout.setColumnStretch(i, s)
        
        # 行のストレッチ設定（適宜調整）
        row_stretch = [1, 1, 1, 2, 2, 1, 1, 1, 2, 2, 1, 1, 1, 1, 1]
        for i, s in enumerate(row_stretch): layout.setRowStretch(i, s)

        # UIセクション構築
        self._add_section(layout, "変調の設定", self.configs[0:3], 0, has_extras=True)
        self._add_section(layout, "復調の設定", self.configs[3:6], 5, has_extras=True)
        self._add_pid_section(layout, "PIDの設定", self.configs[6:], 10)

    def _add_section(self, layout, title, configs, start_row, has_extras=False):
        # タイトル
        lbl = QLabel(title); lbl.setFont(self.header_font); lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl, start_row, 0, 1, 2)
        
        # 各パラメータ (周波数、振幅、オフセット)
        shortcut_keys = [('W','S'), ('E','D'), ('R','F')] if "変調" in title else [('T','G'), ('Y','H'), ('U','J')]
        
        for i, cfg in enumerate(configs):
            # ラベル・テキストボックス
            layout.addWidget(QLabel(f"{cfg.label} ({cfg.unit})", font=self.header_font, alignment=Qt.AlignCenter), start_row+1, i)
            txt = QLineEdit(f"{cfg.init_val:.1f}")
            txt.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            txt.setValidator(QDoubleValidator())
            txt.editingFinished.connect(lambda n=cfg.name: self._on_txt_edited(n))
            txt.installEventFilter(self)
            layout.addWidget(txt, start_row+2, i)
            self.widgets[cfg.name] = txt
            
            # Up/Downボタン
            up_k, dn_k = shortcut_keys[i]
            btn_up = self._create_btn(f"Up({up_k.lower()})", lambda _, n=cfg.name: self._change_value(n, True))
            btn_dn = self._create_btn(f"Down({dn_k.lower()})", lambda _, n=cfg.name: self._change_value(n, False))
            layout.addWidget(btn_up, start_row+3, i)
            layout.addWidget(btn_dn, start_row+4, i)

        if has_extras:
            layout.addWidget(QLabel("その他", font=self.header_font, alignment=Qt.AlignCenter), start_row+1, 4)
            if "変調" in title:
                self.btn_fine = self._create_btn("微調(Shift)", self._toggle_fine_mode, checkable=True)
                self.btn_out = self._create_btn("出力(Space)", self._toggle_mod_output, checkable=True)
                layout.addWidget(self.btn_fine, start_row+3, 4)
                layout.addWidget(self.btn_out, start_row+4, 4)

    def _add_pid_section(self, layout, title, configs, start_row):
        # --- タイトル (Row 10) ---
        lbl = QLabel(title); lbl.setFont(self.header_font); lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl, start_row, 0, 1, 5)
        
        # --- PZT用PID設定 (Row 11-12) ---
        # 最初の4つ (pid_com, pid_p, pid_i, pid_d)
        for i, cfg in enumerate(configs[0:4]):
            layout.addWidget(QLabel(f"{cfg.label}", font=self.header_font, alignment=Qt.AlignCenter), start_row+1, i)
            self.widgets[cfg.name] = self._create_edit(cfg)
            layout.addWidget(self.widgets[cfg.name], start_row+2, i)

        # PID出力ボタン (Row 11-12, 4列目)
        self.btn_pid_out = self._create_btn("PID出力 (X)", self._toggle_pid_output, checkable=True)
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
        self.btn_pid_reset = self._create_btn("積分リセット (C)", self._reset_integrator)
        layout.addWidget(self.btn_pid_reset, start_row + 3, 4, 2, 1)

        # --- LD中心引き戻し設定 (Row 15-16) ---
        # セクション見出し
        lbl_center = QLabel("LD中心引き戻し設定"); lbl_center.setFont(self.header_font)
        layout.addWidget(lbl_center, start_row+5, 0, 1, 3)

        # Center-P, I, D (configs[4:7])
        # ここで enumerate(configs[4:7]) とすることで、4番目(P), 5番目(I), 6番目(D) を回します
        for i, cfg in enumerate(configs[4:7]):
            layout.addWidget(QLabel(f"{cfg.label}", font=self.header_font, alignment=Qt.AlignCenter), start_row+6, i)
            self.widgets[cfg.name] = self._create_edit(cfg)
            layout.addWidget(self.widgets[cfg.name], start_row+7, i)

        # Center用ボタン (Row 16-17, 4列目)
        self.btn_center = self._create_btn("Auto Center (V)", self._toggle_centering, checkable=True)
        self.btn_center_reset = self._create_btn("Centerリセット (B)", self._reset_centering)
        layout.addWidget(self.btn_center, start_row + 6, 4)
        layout.addWidget(self.btn_center_reset, start_row + 7, 4)

        # Centerモニター (Row 18-19)
        layout.addWidget(QLabel("Center操作量(mV)"), start_row+8, 0)
        self.lbl_center_out = QLabel("+0.00E+00"); self.lbl_center_out.setStyleSheet(monitor_style)
        layout.addWidget(self.lbl_center_out, start_row+9, 0)

        layout.addWidget(QLabel("Center積分値"), start_row+8, 1)
        self.lbl_center_ival = QLabel("+0.00E+00"); self.lbl_center_ival.setStyleSheet(monitor_style)
        layout.addWidget(self.lbl_center_ival, start_row+9, 1)
    
    def _create_edit(self, cfg):
        """QLineEdit作成の共通処理"""
        txt = QLineEdit(f"{cfg.init_val:.1f}")
        txt.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        txt.setValidator(QDoubleValidator())
        txt.editingFinished.connect(lambda n=cfg.name: self._on_txt_edited(n))
        txt.installEventFilter(self)
        return txt

    def _create_btn(self, text, callback, checkable=False):
        btn = QPushButton(text)
        btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        btn.setFocusPolicy(Qt.NoFocus)
        if checkable: btn.setCheckable(True)
        btn.clicked.connect(callback)
        return btn

    # --- 特殊トグル処理 ---
    def _toggle_fine_mode(self):
        self.is_fine_mode = self.btn_fine.isChecked()
        style = "background-color: lightblue; font-weight: bold;" if self.is_fine_mode else ""
        self.btn_fine.setStyleSheet(style)
        print(f"Fine mode: {'ON' if self.is_fine_mode else 'OFF'}")

    def _toggle_mod_output(self):
        self.is_output_enabled = self.btn_out.isChecked()
        if self.is_output_enabled:
            self.btn_out.setStyleSheet("background-color: #ff4b4b; color: white; font-weight: bold;")
            self.btn_out.setText("出力中 (Space)")
            self.rp_asg.output_direct = "out1"
            # 現在の値を再適用
            for n in ["mod_freq", "mod_amp", "mod_off"]: self._apply_value(n, self.params[n])
        else:
            self.btn_out.setStyleSheet("")
            self.btn_out.setText("出力(Space)")
            self.rp_asg.output_direct = "off"
            self.rp_asg.frequency = 0; self.rp_asg.offset = 0; self.rp_asg.amplitude = 0
        print(f"Output: {'ENABLED' if self.is_output_enabled else 'DISABLED'}")
        
    def _toggle_pid_output(self):
        self.is_pid_enabled = self.btn_pid_out.isChecked()
        
        # 1. ゲイン設定をハードウェアに再適用
        # これにより、ONにする直前にIゲインが設定値になり、OFFにした瞬間に0になる
        self._update_pid_all()
        
        if self.is_pid_enabled:
            self.btn_pid_out.setStyleSheet("background-color: #ff4b4b; color: white; font-weight: bold;")
            self.btn_pid_out.setText("PID出力中 (X)")
            if hasattr(self, 'rp_pid'):
                self.rp_pid.output_direct = 'out2'
        else:
            self.btn_pid_out.setStyleSheet("")
            self.btn_pid_out.setText("PID出力 (X)")
            if hasattr(self, 'rp_pid'):
                self.rp_pid.output_direct = 'off'
        
        print(f"PID Output: {'ENABLED' if self.is_pid_enabled else 'DISABLED'} (I-Gain Sync Done)")
        
    def _toggle_centering(self):
        self.is_centering_enabled = self.btn_center.isChecked()
        self.btn_center.setStyleSheet("background-color: #f0ad4e; color: black; font-weight: bold;" if self.is_centering_enabled else "")

    def _reset_centering(self):
        self.center_err_sum = 0.0
        self.center_last_err = 0.0
        print("Centering Integrator Reset.")

    def _update_monitors(self):
        if not hasattr(self, 'rp_pid'): return
        try:
            # 1. PZT状態表示
            v_pzt = self.rp_pid.current_output_signal
            self.lbl_pid_out.setText(f"{v_pzt:+.2E}")
            self.lbl_pid_ival.setText(f"{self.rp_pid.ival:+.2E}")

            # 2. ソフトウェアPID (LD中心引き戻し)
            if self.is_centering_enabled and self.is_pid_enabled:
                dt = 0.1 # 100ms
                error = 0.0 - v_pzt # 目標は0V
                
                if self.params["center_i"] != 0:
                    self.center_err_sum += error * dt
                
                derivative = (error - self.center_last_err) / dt
                
                # PID計算
                p_part = self.params["center_p"] * error
                i_part = self.params["center_i"] * self.center_err_sum
                d_part = self.params["center_d"] * derivative
                adjustment = p_part + i_part + d_part
                
                # mod_off 適用
                new_mod_off = self.params["mod_off"] + adjustment
                cfg_mod = next(c for c in self.configs if c.name == "mod_off")
                new_mod_off = max(cfg_mod.min_val, min(cfg_mod.max_val, new_mod_off))
                self._apply_value("mod_off", new_mod_off)
                
                # モニター更新
                self.lbl_center_out.setText(f"{adjustment:+.2E}")
                self.lbl_center_ival.setText(f"{self.center_err_sum:+.2E}")
                
                self.center_last_err = error
            else:
                self.lbl_center_out.setText(f"{0.0:+.2E}")

        except Exception as e:
            print(f"Monitor error: {e}")

    # --- ハードウェア・システム系 ---
    def _initialize_hardware(self):
        print(f"Connecting to {self.HOSTNAME}...")
        try:
            self.rp = RedPitaya(hostname=self.HOSTNAME)
            self.rp_asg = self.rp.asg1
            self.rp_iq = self.rp.iq0
            self.rp_pid = self.rp.pid1
            
            self.rp_asg.setup(waveform='sin', frequency=0, amplitude=0, offset=0, output_direct='off', trigger_source='immediately')
            self.rp_iq.setup(frequency=self.params["mod_freq"], bandwidth=[self.params["demod_cutoff"]], gain=0, 
                             phase=self.params["demod_phase"], input='in1', output_direct='off', 
                             output_signal='quadrature', acbandwidth=0, quadrature_factor=self.params["demod_gain"])
            self.rp_pid.setup(input='iq0', output_direct='off', p=self.params["pid_p"], i=self.params["pid_i"], 
                              d=self.params["pid_d"], ival=0, inputfilter=[])
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
        QShortcut(QKeySequence("V"), self).activated.connect(self.btn_center.click)
        QShortcut(QKeySequence("B"), self).activated.connect(self.btn_center_reset.click)

    def eventFilter(self, source, event):
        """テキストボックスフォーカス中のキー横取り (元のコードの挙動を維持)"""
        if event.type() == QEvent.KeyPress and source in self.widgets.values():
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
            if event.key() == Qt.Key_V: self.btn_center.click(); return True
            if event.key() == Qt.Key_B: self.btn_center_reset.click(); return True
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