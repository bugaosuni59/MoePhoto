'''
super slomo
code refered from https://github.com/avinashpaliwal/Super-SloMo.git
'''
# pylint: disable=E1101
import torch
from torch.nn import ReflectionPad2d
#from torchvision.transforms import Normalize
from slomo import UNet, backWarp
from imageProcess import genGetModel, initModel
from config import config

modelPath = './model/slomo/SuperSloMo.ckpt'
ramCoef = 1
#mean = [0.429, 0.431, 0.397]
#std  = [1, 1, 1]
#negMean = [-x for x in mean]
#identity = lambda x, *_: x
upTruncBy32 = lambda x: (-x & 0xffffffe0 ^ 0xffffffff) + 1
getFlowComp = genGetModel(lambda *_: UNet(6, 4))
getFlowIntrp = genGetModel(lambda *_: UNet(20, 5))
def getFlowBack(opt, width, height):
  if opt.flowBackWarp:
    return opt.flowBackWarp
  opt.flowBackWarp = initModel(backWarp(width, height, config.device(), config.dtype()))
  return opt.flowBackWarp

def getOpt(option):
  def opt():pass
  # Initialize model
  opt.model = modelPath
  dict1 = torch.load(modelPath, map_location='cpu')
  opt.flowComp = initModel(getFlowComp(opt), dict1['state_dictFC'])
  opt.ArbTimeFlowIntrp = initModel(getFlowIntrp(opt), dict1['state_dictAT'])
  opt.flowBackWarp = 0
  opt.sf = option['sf']
  opt.firstTime = 1
  opt.notLast = 1
  if opt.sf < 2:
    raise RuntimeError('Error: --sf/slomo factor has to be at least 2')
  return opt

def getBatchSize(option):
  return max(1, 1 * ramCoef)

def doSlomo(func, node):
  # Temporary fix for issue #7 https://github.com/avinashpaliwal/Super-SloMo/issues/7 -
  # - Removed per channel mean subtraction for CPU.

  def f(data, opt):
    node.reset()
    node.trace(0, p='slomo start')
    _, oriHeight, oriWidth = data[0][0].size()
    width = upTruncBy32(oriWidth)
    height = upTruncBy32(oriHeight)
    pad = ReflectionPad2d((0, width - oriWidth, 0, height - oriHeight))
    unpad = lambda im: im[:, :oriHeight, :oriWidth]
    flowBackWarp = getFlowBack(opt, width, height)

    batchSize = len(data)
    sf = opt.sf
    tempOut = [0 for _ in range(batchSize * sf + 1)]
    # Save reference frames
    if opt.notLast or opt.firstTime:
      tempOut[0] = func(data[0][0])
      outStart = 0
    else:
      outStart = 1
    for i, frames in enumerate(data):
      tempOut[(i + 1) * sf] = frames[1]

    # Load data
    I0 = pad(torch.stack([frames[0] for frames in data]))
    I1 = pad(torch.stack([frames[1] for frames in data]))
    flowOut = opt.flowComp(torch.cat((I0, I1), dim=1))
    F_0_1 = flowOut[:,:2,:,:]
    F_1_0 = flowOut[:,2:,:,:]
    node.trace()

    # Generate intermediate frames
    for intermediateIndex in range(1, sf):
      t = intermediateIndex / sf
      temp = -t * (1 - t)
      fCoeff = (temp, t * t, (1 - t) * (1 - t), temp)
      wCoeff = (1 - t, t)

      F_t_0 = fCoeff[0] * F_0_1 + fCoeff[1] * F_1_0
      F_t_1 = fCoeff[2] * F_0_1 + fCoeff[3] * F_1_0

      g_I0_F_t_0 = flowBackWarp(I0, F_t_0)
      g_I1_F_t_1 = flowBackWarp(I1, F_t_1)

      intrpOut = opt.ArbTimeFlowIntrp(torch.cat((I0, I1, F_0_1, F_1_0, F_t_1, F_t_0, g_I1_F_t_1, g_I0_F_t_0), dim=1))

      F_t_0_f = intrpOut[:, :2, :, :] + F_t_0
      F_t_1_f = intrpOut[:, 2:4, :, :] + F_t_1
      V_t_0   = torch.sigmoid(intrpOut[:, 4:5, :, :])
      V_t_1   = 1 - V_t_0

      g_I0_F_t_0_f = flowBackWarp(I0, F_t_0_f)
      g_I1_F_t_1_f = flowBackWarp(I1, F_t_1_f)

      Ft_p = (wCoeff[0] * V_t_0 * g_I0_F_t_0_f + wCoeff[1] * V_t_1 * g_I1_F_t_1_f) / (wCoeff[0] * V_t_0 + wCoeff[1] * V_t_1)

      # Save intermediate frame
      for i in range(batchSize):
        tempOut[intermediateIndex + i * sf] = unpad(Ft_p[i].detach())

      node.trace()
      tempOut[intermediateIndex] = func(tempOut[intermediateIndex])

    for i in range(sf, len(tempOut)):
      tempOut[i] = func(tempOut[i])
    res = []
    for item in tempOut[outStart:]:
      if type(item) == list:
        res.extend(item)
      elif type(item) != type(None):
        res.append(item)
    opt.firstTime = 0
    return res
  return f