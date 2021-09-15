from collections import namedtuple
from cereal import car
from common.realtime import DT_CTRL
from selfdrive.controls.lib.drive_helpers import rate_limit
from common.numpy_fast import clip, interp
from selfdrive.car import create_gas_command
from selfdrive.car.honda import hondacan
from selfdrive.car.honda.values import OLD_NIDEC_LONG_CONTROL, CruiseButtons, CAR, VISUAL_HUD, HONDA_BOSCH, CarControllerParams
from opendbc.can.packer import CANPacker
from common.dp_common import common_controller_ctrl

VisualAlert = car.CarControl.HUDControl.VisualAlert


# TODO: not clear this does anything useful
def actuator_hystereses(brake, braking, brake_steady, v_ego, car_fingerprint):
  # hyst params
  brake_hyst_on = 0.02     # to activate brakes exceed this value
  brake_hyst_off = 0.005   # to deactivate brakes below this value
  brake_hyst_gap = 0.01    # don't change brake command for small oscillations within this value

  #*** hysteresis logic to avoid brake blinking. go above 0.1 to trigger
  if (brake < brake_hyst_on and not braking) or brake < brake_hyst_off:
    brake = 0.
  braking = brake > 0.

  # for small brake oscillations within brake_hyst_gap, don't change the brake command
  if brake == 0.:
    brake_steady = 0.
  elif brake > brake_steady + brake_hyst_gap:
    brake_steady = brake - brake_hyst_gap
  elif brake < brake_steady - brake_hyst_gap:
    brake_steady = brake + brake_hyst_gap
  brake = brake_steady

  if (car_fingerprint in (CAR.ACURA_ILX, CAR.CRV, CAR.CRV_EU)) and brake > 0.0:
    brake += 0.15

  return brake, braking, brake_steady


def brake_pump_hysteresis(apply_brake, apply_brake_last, last_pump_ts, ts):
  pump_on = False

  # reset pump timer if:
  # - there is an increment in brake request
  # - we are applying steady state brakes and we haven't been running the pump
  #   for more than 20s (to prevent pressure bleeding)
  if apply_brake > apply_brake_last or (ts - last_pump_ts > 20. and apply_brake > 0):
    last_pump_ts = ts

  # once the pump is on, run it for at least 0.2s
  if ts - last_pump_ts < 0.2 and apply_brake > 0:
    pump_on = True

  return pump_on, last_pump_ts


def process_hud_alert(hud_alert):
  # initialize to no alert
  fcw_display = 0
  steer_required = 0
  acc_alert = 0

  # priority is: FCW, steer required, all others
  if hud_alert == VisualAlert.fcw:
    fcw_display = VISUAL_HUD[hud_alert.raw]
  elif hud_alert in [VisualAlert.steerRequired, VisualAlert.ldw]:
    steer_required = VISUAL_HUD[hud_alert.raw]
  else:
    acc_alert = VISUAL_HUD[hud_alert.raw]

  return fcw_display, steer_required, acc_alert


HUDData = namedtuple("HUDData",
                     ["pcm_accel", "v_cruise", "car",
                     "lanes", "fcw", "acc_alert", "steer_required", "dist_lines", "dashed_lanes"])


class CarController():
  def rough_speed(self, lead_distance):
    if self.prev_lead_distance != lead_distance:
      self.lead_distance_counter_prev = self.lead_distance_counter
      self.rough_lead_speed += 0.3334 * (
              (lead_distance - self.prev_lead_distance) / self.lead_distance_counter_prev - self.rough_lead_speed)
      self.lead_distance_counter = 0.0
    elif self.lead_distance_counter >= self.lead_distance_counter_prev:
      self.rough_lead_speed = (self.lead_distance_counter * self.rough_lead_speed) / (self.lead_distance_counter + 1.0)
    self.lead_distance_counter += 1.0
    self.prev_lead_distance = lead_distance
    return self.rough_lead_speed


  def __init__(self, dbc_name, CP, VM):
    # dp
    self.last_blinker_on = False
    self.blinker_end_frame = 0.
    self.prev_lead_distance = 0.0
    self.stopped_lead_distance = 0.0
    self.lead_distance_counter = 1
    self.lead_distance_counter_prev = 1
    self.rough_lead_speed = 0.0

    self.braking = False
    self.brake_steady = 0.
    self.brake_last = 0.
    self.signal_last = 0.
    self.apply_brake_last = 0
    self.last_pump_ts = 0.
    self.packer = CANPacker(dbc_name)

    self.params = CarControllerParams(CP)

  def update(self, enabled, CS, frame, actuators,
             pcm_speed, pcm_override, pcm_cancel_cmd, pcm_accel,
             hud_v_cruise, hud_show_lanes, dragonconf, hud_show_car, hud_alert):

    P = self.params

    # *** apply brake hysteresis ***
    brake, self.braking, self.brake_steady = actuator_hystereses(actuators.brake, self.braking, self.brake_steady, CS.out.vEgo, CS.CP.carFingerprint)

    # *** no output if not enabled ***
    if not enabled and CS.out.cruiseState.enabled:
      # send pcm acc cancel cmd if drive is disabled but pcm is still on, or if the system can't be activated
      pcm_cancel_cmd = True

    # Never send cancel command if we never enter cruise state (no cruise if pedal)
    # Cancel cmd causes brakes to release at a standstill causing grinding
    pcm_cancel_cmd = pcm_cancel_cmd and CS.CP.pcmCruise

    # *** rate limit after the enable check ***
    self.brake_last = rate_limit(brake, self.brake_last, -2., DT_CTRL)

    if enabled and CS.out.cruiseState.enabled:
      if hud_show_car:
        hud_car = 2
      else:
        hud_car = 1
    else:
      hud_car = 0

    fcw_display, steer_required, acc_alert = process_hud_alert(hud_alert)

    cur_time = frame * DT_CTRL
    if (CS.leftBlinkerOn or CS.rightBlinkerOn):
      self.signal_last = cur_time

    lkas_active = enabled and not CS.steer_not_allowed and CS.lkasEnabled and ((CS.automaticLaneChange and not CS.belowLaneChangeSpeed) or ((not ((cur_time - self.signal_last) < 1) or not CS.belowLaneChangeSpeed) and not (CS.leftBlinkerOn or CS.rightBlinkerOn)))

    # **** process the car messages ****

    # steer torque is converted back to CAN reference (positive when steering right)
    apply_steer = int(interp(-actuators.steer * P.STEER_MAX, P.STEER_LOOKUP_BP, P.STEER_LOOKUP_V))

    # Send CAN commands.
    can_sends = []

    # tester present - w/ no response (keeps radar disabled)
    if CS.CP.carFingerprint in HONDA_BOSCH and CS.CP.openpilotLongitudinalControl:
      if (frame % 10) == 0:
        can_sends.append((0x18DAB0F1, 0, b"\x02\x3E\x80\x00\x00\x00\x00\x00", 1))

    # dp
    blinker_on = CS.out.leftBlinker or CS.out.rightBlinker
    if not enabled:
      self.blinker_end_frame = 0
    if self.last_blinker_on and not blinker_on:
      self.blinker_end_frame = frame + dragonconf.dpSignalOffDelay
    apply_steer = common_controller_ctrl(enabled,
                                         dragonconf,
                                         blinker_on or frame < self.blinker_end_frame,
                                         apply_steer, CS.out.vEgo)
    self.last_blinker_on = blinker_on

    # Send steering command.
    idx = frame % 4
    can_sends.append(hondacan.create_steering_control(self.packer, apply_steer,
      lkas_active, CS.CP.carFingerprint, idx, CS.CP.openpilotLongitudinalControl))


    accel = actuators.gas - actuators.brake

    # TODO: pass in LoC.long_control_state and use that to decide starting/stoppping
    stopping = accel < 0 and CS.out.vEgo < 0.3
    starting = accel > 0 and CS.out.vEgo < 0.3

    # Prevent rolling backwards
    accel = -1.0 if stopping else accel
    if CS.CP.carFingerprint in HONDA_BOSCH:
      apply_accel = interp(accel, P.BOSCH_ACCEL_LOOKUP_BP, P.BOSCH_ACCEL_LOOKUP_V)
    else:
      apply_accel = interp(accel, P.NIDEC_ACCEL_LOOKUP_BP, P.NIDEC_ACCEL_LOOKUP_V)

    # wind brake from air resistance decel at high speed
    wind_brake = interp(CS.out.vEgo, [0.0, 2.3, 35.0], [0.001, 0.002, 0.15])
    if CS.CP.enableGasInterceptor or CS.CP.carFingerprint in OLD_NIDEC_LONG_CONTROL:
      #pcm_speed = pcm_speed
      pcm_accel = int(clip(pcm_accel, 0, 1) * 0xc6)
    else:
      max_accel = interp(CS.out.vEgo, P.NIDEC_MAX_ACCEL_BP, P.NIDEC_MAX_ACCEL_V)
      pcm_accel = int(clip(apply_accel/max_accel, 0.0, 1.0) * 0xc6)
      pcm_speed_BP = [-wind_brake,
                      -wind_brake*(3/4),
                      0.0]
      pcm_speed_V = [0.0,
                     clip(CS.out.vEgo + apply_accel/2.0 - 2.0, 0.0, 100.0),
                     clip(CS.out.vEgo + apply_accel/2.0 + 2.0, 0.0, 100.0)]
      pcm_speed = interp(accel, pcm_speed_BP, pcm_speed_V)

    if not CS.CP.openpilotLongitudinalControl:
      if (frame % 2) == 0:
        idx = frame // 2
        can_sends.append(hondacan.create_bosch_supplemental_1(self.packer, CS.CP.carFingerprint, idx))
      # If using stock ACC, spam cancel command to kill gas when OP disengages.
      if not dragonconf.dpAllowGas and pcm_cancel_cmd:
        can_sends.append(hondacan.spam_buttons_command(self.packer, CruiseButtons.CANCEL, idx, CS.CP.carFingerprint))
      elif CS.out.cruiseState.standstill:
        if CS.CP.carFingerprint in (CAR.ACCORD, CAR.ACCORDH, CAR.INSIGHT):
          rough_lead_speed = self.rough_speed(CS.lead_distance)
          if CS.lead_distance > (self.stopped_lead_distance + 15.0) or rough_lead_speed > 0.1:
            self.stopped_lead_distance = 0.0
            can_sends.append(
              hondacan.spam_buttons_command(self.packer, CruiseButtons.RES_ACCEL, idx, CS.CP.carFingerprint))
        elif CS.CP.carFingerprint in (CAR.CIVIC_BOSCH, CAR.CRV_HYBRID):
          if CS.hud_lead == 1:
            can_sends.append(hondacan.spam_buttons_command(self.packer, CruiseButtons.RES_ACCEL, idx, CS.CP.carFingerprint))
        else:
          can_sends.append(hondacan.spam_buttons_command(self.packer, CruiseButtons.RES_ACCEL, idx, CS.CP.carFingerprint))
      else:
        self.stopped_lead_distance = CS.lead_distance
        self.prev_lead_distance = CS.lead_distance

    else:
      # Send gas and brake commands.
      if (frame % 2) == 0:
        idx = frame // 2
        ts = frame * DT_CTRL

        if CS.CP.carFingerprint in HONDA_BOSCH:
          apply_gas = interp(accel, P.BOSCH_GAS_LOOKUP_BP, P.BOSCH_GAS_LOOKUP_V)
          if not CS.out.cruiseState.enabled:
            apply_gas = 0.
          if dragonconf.dpAtl and dragonconf.dpAtlOpLong and not CS.out.cruiseActualEnabled:
            apply_gas = 0
            apply_accel = 0
          can_sends.extend(hondacan.create_acc_commands(self.packer, enabled, apply_accel, apply_gas, idx, stopping, starting, CS.CP.carFingerprint))

        else:
          apply_brake = clip(self.brake_last - wind_brake, 0.0, 1.0)
          apply_brake = int(clip(apply_brake * P.BRAKE_MAX, 0, P.BRAKE_MAX - 1))
          if not CS.out.cruiseState.enabled:
            apply_brake = 0.
          if dragonconf.dpAtl and dragonconf.dpAtlOpLong and not CS.out.cruiseActualEnabled:
            apply_brake = 0
          pump_on, self.last_pump_ts = brake_pump_hysteresis(apply_brake, self.apply_brake_last, self.last_pump_ts, ts)
          can_sends.append(hondacan.create_brake_command(self.packer, apply_brake, pump_on,
            pcm_override, pcm_cancel_cmd, fcw_display, idx, CS.CP.carFingerprint, CS.stock_brake))
          self.apply_brake_last = apply_brake

          if CS.CP.enableGasInterceptor:
            # way too aggressive at low speed without this
            gas_mult = interp(CS.out.vEgo, [0., 10.], [0.4, 1.0])
            # send exactly zero if apply_gas is zero. Interceptor will send the max between read value and apply_gas.
            # This prevents unexpected pedal range rescaling
            apply_gas = clip(gas_mult * actuators.gas, 0., 1.)
            if not CS.out.cruiseState.enabled:
              apply_gas = 0.
            if dragonconf.dpAtl and dragonconf.dpAtlOpLong and CS.out.cruiseActualEnabled:
              apply_gas = 0
            can_sends.append(create_gas_command(self.packer, apply_gas, idx))

    hud = HUDData(int(pcm_accel), (int(round(hud_v_cruise)) if hud_car != 0 else 255), hud_car,
                  hud_show_lanes and lkas_active, fcw_display, acc_alert, steer_required, CS.read_distance_lines, CS.lkasEnabled and not lkas_active)

    # Send dashboard UI commands.
    if not dragonconf.dpAtl and (frame % 10) == 0:
      idx = (frame//10) % 4
      can_sends.extend(hondacan.create_ui_commands(self.packer, pcm_speed, hud, CS.CP.carFingerprint, CS.is_metric, idx, CS.CP.openpilotLongitudinalControl, CS.stock_hud))

    return can_sends
