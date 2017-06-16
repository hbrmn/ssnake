#!/usr/bin/env python

# Copyright 2016 - 2017 Bas van Meerten and Wouter Franssen

# This file is part of ssNake.
#
# ssNake is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ssNake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with ssNake. If not, see <http://www.gnu.org/licenses/>.

import numpy as np
try:
    from PyQt4 import QtGui, QtCore
    from PyQt4 import QtGui as QtWidgets
    from matplotlib.backends.backend_qt4agg import FigureCanvasQTAgg as FigureCanvas
except ImportError:
    from PyQt5 import QtGui, QtCore, QtWidgets
    from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import scipy.optimize
import scipy.ndimage
import multiprocessing
import copy
import spectrum_classes
from safeEval import safeEval
from spectrumFrame import Plot1DFrame
from zcw import zcw_angles
from widgetClasses import QLabel
import widgetClasses as wc

import time

pi = np.pi
stopDict = {} #Global dictionary with stopping commands for fits


def isfloat(value):
  try:
    float(value)
    return True
  except ValueError:
    return False

def checkLinkTuple(inp):
    if len(inp) == 2:
        inp += (1, 0, 0)
    elif len(inp) == 3:
        inp += (0, 0)
    return inp

##############################################################################
def shiftConversion(Values, Type):
    #Calculates the chemical shift tensor based on:
    #Values: a list with three numbers
    #Type: an integer defining the input shift convention
    #Returns a list of list with all calculated values

    if Type == 0:  # If from standard
        deltaArray = Values
    if Type == 1:  # If from xyz
        deltaArray = Values  # Treat xyz as 123, as it reorders them anyway
    if Type == 2:  # From haeberlen
        iso = Values[0]
        delta = Values[1]
        eta = Values[2]
        delta11 = delta + iso  # Treat xyz as 123, as it reorders them anyway
        delta22 = (eta * delta + iso * 3 - delta11) / 2.0
        delta33 = iso * 3 - delta11 - delta22
        deltaArray = [delta11,delta22,delta33]
    if Type == 3:  # From Hertzfeld-Berger
        iso = Values[0]
        span = Values[1]
        skew = Values[2]
        delta22 = iso + skew * span / 3.0
        delta33 = (3 * iso - delta22 - span) / 2.0
        delta11 = 3 * iso - delta22 - delta33
        deltaArray = [delta11,delta22,delta33]
    Results = [] #List of list with the different definitions
    # Force right order
    deltaSorted = np.sort(deltaArray)
    D11 = deltaSorted[2]
    D22 = deltaSorted[1]
    D33 = deltaSorted[0]
    Results.append([D11,D22,D33])
    # Convert to haeberlen convention and xxyyzz
    iso = (D11 + D22 + D33) / 3.0
    xyzIndex = np.argsort(np.abs(deltaArray - iso))
    zz = deltaArray[xyzIndex[2]]
    yy = deltaArray[xyzIndex[0]]
    xx = deltaArray[xyzIndex[1]]
    Results.append([xx,yy,zz])
    aniso = zz - iso
    if aniso != 0.0:  # Only is not zero
        eta = (yy - xx) / aniso
    else:
        eta = 'ND'
    Results.append([iso,aniso,eta])    
    # Convert to Herzfeld-Berger Convention
    span = D11 - D33
    if span != 0.0:  # Only if not zero
        skew = 3.0 * (D22 - iso) / span
    else:
        skew = 'ND'
    Results.append([iso,span,skew])
    return Results

def voigtLine(x, pos, lor, gau, integral, Type = 0):
    lor = np.abs(lor)
    gau = np.abs(gau)
    axis = x - pos
    if Type == 0: #Exact: Freq domain simulation via Faddeeva function
        if gau == 0.0: #If no gauss, just take lorentz
           lor = 1.0 / (np.pi * 0.5 * lor * (1 + (axis /(0.5 * lor))**2) )
           return integral * lor
        else:
            sigma = gau / (2 * np.sqrt(2 * np.log(2)))
            z = (axis + 1j * lor / 2) / (sigma * np.sqrt(2))
            return integral * scipy.special.wofz(z).real / (sigma * np.sqrt(2 * np.pi))
    elif Type == 1: #Approximation: THOMPSON et al (doi: 10.1107/S0021889887087090 )
        sigma = gau / (2 * np.sqrt(2 * np.log(2)))
        lb = lor / 2
        f = (sigma**5 + 2.69269 * sigma**4 * lb + 2.42843 * sigma**3 * lb**2 + 4.47163 * sigma**2 * lb**3 + 0.07842* sigma * lb**4 + lb**5) ** 0.2
        eta = 1.36603 * (lb/f) - 0.47719 * (lb/f)**2 + 0.11116 * (lb/f)**3
        lor = f / (np.pi * (axis**2 + f**2))
        gauss = np.exp( -axis**2 / (2 * f**2)) / (f * np.sqrt(2 * np.pi))
        return integral * (eta * lor + (1 - eta) * gauss)

#############################################################################################


class TabFittingWindow(QtWidgets.QWidget):

    PRECIS = 3
    MINMETHOD = 'Powell'

    def __init__(self, mainProgram, oldMainWindow):
        super(TabFittingWindow, self).__init__(mainProgram)
        self.mainProgram = mainProgram
        self.oldMainWindow = oldMainWindow
        self.subFitWindows = []
        self.tabs = QtWidgets.QTabWidget(self)
        self.tabs.setTabPosition(2)
        self.mainFitWindow = FittingWindow(mainProgram, oldMainWindow, self)
        self.current = self.mainFitWindow.current
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self.closeTab)
        self.tabs.addTab(self.mainFitWindow, 'Spectrum') 
        grid3 =  QtWidgets.QGridLayout(self)
        grid3.addWidget(self.tabs,0,0)
        grid3.setColumnStretch(0, 1)
        grid3.setRowStretch(0, 1)

    def addSpectrum(self):
        text = QtWidgets.QInputDialog.getItem(self, "Select spectrum to add", "Spectrum name:", self.mainProgram.workspaceNames, 0, False)
        if text[1]:
            self.subFitWindows.append(FittingWindow(self.mainProgram, self.mainProgram.workspaces[self.mainProgram.workspaceNames.index(text[0])], self, False))
            self.tabs.addTab(self.subFitWindows[-1], str(text[0]))
            self.tabs.setCurrentIndex(len(self.subFitWindows))

    def removeSpectrum(self, spec):
        num = self.subFitWindows.index(spec)
        self.tabs.removeTab(num+1)
        del self.subFitWindows[num]

    def closeTab(self, num):
        if num > 0:
            self.tabs.removeTab(num)
            del self.subFitWindows[num-1]
        else:
            self.mainFitWindow.paramframe.closeWindow()
        
    def fit(self):
        xax, data1D, guess, args, out = self.mainFitWindow.paramframe.getFitParams()
        xax = [xax]
        out = [out]
        nameList = ['Spectrum']
        selectList = [slice(0, len(guess))]
        for i in range(len(self.subFitWindows)):
            xax_tmp, data1D_tmp, guess_tmp, args_tmp, out_tmp = self.subFitWindows[i].paramframe.getFitParams()
            out.append(out_tmp)
            xax.append(xax_tmp)
            nameList.append('bla')
            selectList.append(slice(len(guess), len(guess)+len(guess_tmp)))
            data1D = np.append(data1D, data1D_tmp)
            guess += guess_tmp
            new_args = ()
            for n in range(len(args)):
                new_args += (args[n] + args_tmp[n],)
            args = new_args # tuples are immutable
        new_args = (nameList, selectList) + args
        allFitVal = self.mainFitWindow.paramframe.fit(xax, data1D, guess, new_args)['x']
        fitVal = []
        for length in selectList:
            fitVal.append(allFitVal[length])
        args_out = []
        for n in range(len(args)):
            args_out.append([args[n][0]])
        self.mainFitWindow.paramframe.setResults(fitVal[0], args_out, out[0])
        for i in (range(len(self.subFitWindows))):
            args_out = []
            for n in range(len(args)):
                args_out.append([args[n][i+1]])
            self.subFitWindows[i].paramframe.setResults(fitVal[i+1], args_out, out[i+1])

    def disp(self, *args, **kwargs):
        params = [self.mainFitWindow.paramframe.getSimParams()]
        for window in self.subFitWindows:
            tmp_params = window.paramframe.getSimParams()
            for i in range(len(params)):
                params = np.append(params, [tmp_params], axis=0)
        self.mainFitWindow.paramframe.disp(params, 0, *args, **kwargs)
        for i in range(len(self.subFitWindows)):
            self.subFitWindows[i].paramframe.disp(params, i+1, *args, **kwargs)

    def get_masterData(self):
        return self.oldMainWindow.get_masterData()

    def get_current(self):
        return self.oldMainWindow.get_current()

    def kill(self):
        self.mainFitWindow.paramframe.closeWindow()

##############################################################################

    
class FitCopySettingsWindow(QtWidgets.QWidget):

    def __init__(self, parent, returnFunction, single=False):
        super(FitCopySettingsWindow, self).__init__(parent)
        self.setWindowFlags(QtCore.Qt.Window | QtCore.Qt.Tool)
        self.father = parent
        self.returnFunction = returnFunction
        self.setWindowTitle("Settings")
        layout = QtWidgets.QGridLayout(self)
        grid = QtWidgets.QGridLayout()
        layout.addLayout(grid, 0, 0, 1, 2)
        self.allTraces = QtWidgets.QCheckBox("Export all traces")
        if not single:
            grid.addWidget(self.allTraces, 0, 0)
        self.original = QtWidgets.QCheckBox("Include original")
        self.original.setChecked(True)
        grid.addWidget(self.original, 1, 0)
        self.subFits = QtWidgets.QCheckBox("Include subfits")
        self.subFits.setChecked(True)
        grid.addWidget(self.subFits, 2, 0)
        self.difference = QtWidgets.QCheckBox("Include difference")
        grid.addWidget(self.difference, 3, 0)
        cancelButton = QtWidgets.QPushButton("&Cancel")
        cancelButton.clicked.connect(self.closeEvent)
        layout.addWidget(cancelButton, 2, 0)
        okButton = QtWidgets.QPushButton("&Ok", self)
        okButton.clicked.connect(self.applyAndClose)
        okButton.setFocus()
        layout.addWidget(okButton, 2, 1)
        grid.setRowStretch(100, 1)
        self.show()
        self.setGeometry(self.frameSize().width() - self.geometry().width(), self.frameSize().height() - self.geometry().height(), 0, 0)

    def closeEvent(self, *args):
        self.deleteLater()

    def applyAndClose(self, *args):
        self.deleteLater()
        self.returnFunction([self.allTraces.isChecked(), self.original.isChecked(), self.subFits.isChecked(), self.difference.isChecked()])

##################################################################################################
        
        
class ParamCopySettingsWindow(QtWidgets.QWidget):

    def __init__(self, parent, paramNames, returnFunction, single=False):
        super(ParamCopySettingsWindow, self).__init__(parent)
        self.setWindowFlags(QtCore.Qt.Window | QtCore.Qt.Tool)
        self.father = parent
        self.returnFunction = returnFunction
        self.setWindowTitle("Parameters to export")
        layout = QtWidgets.QGridLayout(self)
        grid = QtWidgets.QGridLayout()
        layout.addLayout(grid, 0, 0, 1, 2)
        self.allTraces = QtWidgets.QCheckBox("Export all traces")
        if not single:
            grid.addWidget(self.allTraces, 0, 0)
        self.exportList = []
        for i in range(len(paramNames)):
            self.exportList.append(QtWidgets.QCheckBox(paramNames[i]))
            grid.addWidget(self.exportList[-1], i+1, 0)
        cancelButton = QtWidgets.QPushButton("&Cancel")
        cancelButton.clicked.connect(self.closeEvent)
        layout.addWidget(cancelButton, 2, 0)
        okButton = QtWidgets.QPushButton("&Ok", self)
        okButton.clicked.connect(self.applyAndClose)
        okButton.setFocus()
        layout.addWidget(okButton, 2, 1)
        grid.setRowStretch(100, 1)
        self.show()
        self.setGeometry(self.frameSize().width() - self.geometry().width(), self.frameSize().height() - self.geometry().height(), 0, 0)

    def closeEvent(self, *args):
        self.deleteLater()

    def applyAndClose(self, *args):
        self.deleteLater()
        answers = []
        for checkbox in self.exportList:
            answers.append(checkbox.isChecked())
        self.returnFunction(self.allTraces.isChecked(), answers)

################################################################################
        
        
class FittingWindow(QtWidgets.QWidget):
    # Inherited by the fitting windows

    def __init__(self, mainProgram, oldMainWindow, tabWindow, isMain=True):
        super(FittingWindow, self).__init__(mainProgram)
        self.isMain = isMain
        self.mainProgram = mainProgram
        self.oldMainWindow = oldMainWindow
        self.tabWindow = tabWindow
        self.fig = Figure()
        self.canvas = FigureCanvas(self.fig)
        grid = QtWidgets.QGridLayout(self)
        grid.addWidget(self.canvas, 0, 0)
        self.current = self.tabWindow.CURRENTWINDOW(self, self.fig, self.canvas, self.oldMainWindow.get_current())
        self.paramframe = self.tabWindow.PARAMFRAME(self.current, self, isMain=self.isMain)

        self.fig.suptitle(self.oldMainWindow.get_masterData().name)
        grid.addWidget(self.paramframe, 1, 0)
        grid.setColumnStretch(0, 1)
        grid.setRowStretch(0, 1)
        self.grid = grid
        self.fittingSideFrame = FittingSideFrame(self)
        self.grid.addWidget(self.fittingSideFrame, 0, 1)
        self.canvas.mpl_connect('button_press_event', self.buttonPress)
        self.canvas.mpl_connect('button_release_event', self.buttonRelease)
        self.canvas.mpl_connect('motion_notify_event', self.pan)
        self.canvas.mpl_connect('scroll_event', self.scroll)

    def fit(self):
        self.tabWindow.fit()

    def sim(self, *args, **kwargs):
        self.tabWindow.disp(**kwargs)
        
    def addSpectrum(self):
        self.tabWindow.addSpectrum()

    def removeSpectrum(self):
        self.tabWindow.removeSpectrum(self)

    def createNewData(self, data, axes, params=False, fitAll=False):
        masterData = self.get_masterData()
        if fitAll:
            if params:
                self.mainProgram.dataFromFit(data,
                                         masterData.filePath,
                                         np.append([masterData.freq[axes], masterData.freq[axes]], np.delete(masterData.freq, axes)),
                                         np.append([masterData.sw[axes],masterData.sw[axes]], np.delete(masterData.sw, axes)),
                                         np.append([False, False], np.delete(masterData.spec, axes)),
                                         np.append([False, False], np.delete(masterData.wholeEcho, axes)),
                                         np.append([None, None], np.delete(masterData.ref, axes)),
                                         None,
                                         axes+1)
            else:
                self.mainProgram.dataFromFit(data,
                                         masterData.filePath,
                                         np.append(masterData.freq[axes], masterData.freq),
                                         np.append(masterData.sw[axes], masterData.sw),
                                         np.append(False, masterData.spec),
                                         np.append(False, masterData.wholeEcho),
                                         np.append(None, masterData.ref),
                                             None,
                                         axes+1)
        else:
            if params:
                self.mainProgram.dataFromFit(data,
                                             masterData.filePath,
                                             [masterData.freq[axes], masterData.freq[axes]],
                                             [masterData.sw[axes], masterData.sw[axes]],
                                             [False, False],
                                             [False, False],
                                             [None, None],
                                             [np.arange(data.shape[0]), np.arange(data.shape[1])],
                                             0)
            else:
                self.mainProgram.dataFromFit(data,
                                             masterData.filePath,
                                             [masterData.freq[axes], masterData.freq[axes]],
                                             [masterData.sw[axes], masterData.sw[axes]],
                                             [False, masterData.spec[axes]],
                                             [False, masterData.wholeEcho[axes]],
                                             [None, masterData.ref[axes]],
                                             [np.arange(len(data)), masterData.xaxArray[axes]],
                                             axes)

    def rename(self, name):
        self.fig.suptitle(name)
        self.canvas.draw()
        self.oldMainWindow.rename(name)

    def buttonPress(self, event):
        self.current.buttonPress(event)

    def buttonRelease(self, event):
        self.current.buttonRelease(event)

    def pan(self, event):
        self.current.pan(event)

    def scroll(self, event):
        self.current.scroll(event)

    def get_mainWindow(self):
        return self.oldMainWindow

    def get_masterData(self):
        return self.oldMainWindow.get_masterData()

    def get_current(self):
        return self.oldMainWindow.get_current()

    def kill(self):
        for i in reversed(range(self.grid.count())):
            self.grid.itemAt(i).widget().deleteLater()
        self.grid.deleteLater()
        self.current.kill()
        self.oldMainWindow.kill()
        del self.current
        del self.paramframe
        del self.fig
        del self.canvas
        self.deleteLater()

    def cancel(self):
        for i in reversed(range(self.grid.count())):
            self.grid.itemAt(i).widget().deleteLater()
        self.grid.deleteLater()
        del self.current
        del self.paramframe
        del self.canvas
        del self.fig
        self.mainProgram.closeFitWindow(self.oldMainWindow)
        self.deleteLater()

##############################################################################


class FittingSideFrame(QtWidgets.QScrollArea):

    def __init__(self, parent):
        super(FittingSideFrame, self).__init__(parent)
        self.father = parent
        self.entries = []
        content = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(content)
        grid.setSizeConstraint(QtWidgets.QLayout.SetFixedSize)
        frame1Widget = QtWidgets.QWidget()
        frame2Widget = QtWidgets.QWidget()
        grid.addWidget(frame1Widget, 0, 0)
        grid.addWidget(frame2Widget, 1, 0)
        self.frame1 = QtWidgets.QGridLayout()
        self.frame2 = QtWidgets.QGridLayout()
        frame1Widget.setLayout(self.frame1)
        frame2Widget.setLayout(self.frame2)
        self.frame1.setAlignment(QtCore.Qt.AlignTop)
        self.frame2.setAlignment(QtCore.Qt.AlignTop)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.grid = grid
        self.setWidget(content)
        self.upd()

    def kill(self):
        for i in reversed(range(self.grid.count())):
            self.grid.itemAt(i).widget().deleteLater()
        self.grid.deleteLater()
        
    def upd(self):
        current = self.father.current
        self.shape = current.data.data.shape
        self.length = len(self.shape)
        for i in reversed(range(self.frame1.count())):
            item = self.frame1.itemAt(i).widget()
            self.frame1.removeWidget(item)
            item.deleteLater()
        for i in reversed(range(self.frame2.count())):
            item = self.frame2.itemAt(i).widget()
            self.frame2.removeWidget(item)
            item.deleteLater()
        self.entries = []
        if self.length > 1:
            for num in range(self.length):
                self.frame1.addWidget(wc.QLabel("D" + str(num + 1), self), num * 2, 0)
                self.entries.append(wc.SliceSpinBox(self, 0, self.shape[num] - 1))
                self.frame1.addWidget(self.entries[num], num * 2 + 1, 0)
                if num < current.axes:
                    self.entries[num].setValue(current.locList[num])
                elif num == current.axes:
                    self.entries[num].setValue(0)
                    self.entries[num].setDisabled(True)
                else:
                    self.entries[num].setValue(current.locList[num - 1])
                self.entries[num].valueChanged.connect(lambda event=None, num=num: self.getSlice(event, num))
        QtCore.QTimer.singleShot(100, self.resizeAll)

    def resizeAll(self):
        self.setMinimumWidth(self.grid.sizeHint().width() + self.verticalScrollBar().sizeHint().width())

    def getSlice(self, event, entryNum):
        if entryNum == self.father.current.axes:
            return
        else:
            dimNum = self.father.current.axes
        locList = []
        for num in range(self.length):
            inp = self.entries[num].value()
            if num == dimNum:
                pass
            else:
                locList.append(inp)
        self.father.current.setSlice(dimNum, locList)

#################################################################################


class FitPlotFrame(Plot1DFrame):

    MARKER = ''
    LINESTYLE = '-'
    FITNUM = 10 # Standard number of fits

    def __init__(self, rootwindow, fig, canvas, current):
        super(FitPlotFrame, self).__init__(rootwindow, fig, canvas)
        self.data1D = current.getDisplayedData()
        self.data = current.data
        self.axes = current.axes
        if (len(current.locList) == self.data.data.ndim - 1):
            self.locList = current.locList
        else:
            if self.axes < current.axes2:
                self.locList = np.insert(current.locList, current.axes2 - 1, 0)
            else:
                self.locList = np.insert(current.locList, current.axes2, 0)
        self.current = current
        self.spec = self.current.spec
        self.xax = self.current.xax
        tmp = list(self.data.data.shape)
        tmp.pop(self.axes)
        self.fitDataList = np.full(tmp, None, dtype=object)
        self.fitPickNumList = np.zeros(tmp, dtype=int)
        self.plotType = 0
        self.rootwindow = rootwindow
        #Set limits as in parent
        self.xminlim = self.current.xminlim
        self.xmaxlim = self.current.xmaxlim
        self.yminlim = self.current.yminlim
        self.ymaxlim = self.current.ymaxlim
        if isinstance(self.current, spectrum_classes.CurrentContour):
            self.plotReset(False, True)
        self.showFid()

    def setSlice(self, axes, locList):
        self.rootwindow.paramframe.checkInputs()
        self.pickWidth = False
        if self.axes != axes:
            return
        self.locList = locList
        self.upd()
        self.rootwindow.paramframe.checkFitParamList(tuple(self.locList))
        self.rootwindow.paramframe.dispParams()
        self.showFid()

    def upd(self):  
        updateVar = self.data.getSlice(self.axes, self.locList)
        tmp = updateVar[0]
        if self.current.plotType == 0:
            self.data1D = np.real(tmp)
        elif self.current.plotType == 1:
            self.data1D = np.imag(tmp)
        elif self.current.plotType == 2:
            self.data1D = np.real(tmp)
        elif self.current.plotType == 3:
            self.data1D = np.abs(tmp)
        
    def plotReset(self, xReset=True, yReset=True):
        a = self.fig.gca()
        if self.plotType == 0:
            miny = min(np.real(self.data1D))
            maxy = max(np.real(self.data1D))
        elif self.plotType == 1:
            miny = min(np.imag(self.data1D))
            maxy = max(np.imag(self.data1D))
        elif self.plotType == 2:
            miny = min(min(np.real(self.data1D)), min(np.imag(self.data1D)))
            maxy = max(max(np.real(self.data1D)), max(np.imag(self.data1D)))
        elif self.plotType == 3:
            miny = min(np.abs(self.data1D))
            maxy = max(np.abs(self.data1D))
        else:
            miny = -1
            maxy = 1
        differ = 0.05 * (maxy - miny)
        if yReset:
            self.yminlim = miny - differ
            self.ymaxlim = maxy + differ
        if self.spec == 1:
            if self.current.ppm:
                axMult = 1e6 / self.current.ref
            else:
                axMult = 1.0 / (1000.0**self.current.axType)
        elif self.spec == 0:
            axMult = 1000.0**self.current.axType
        if xReset:
            self.xminlim = min(self.xax * axMult)
            self.xmaxlim = max(self.xax * axMult)
        if self.spec > 0:
            a.set_xlim(self.xmaxlim, self.xminlim)
        else:
            a.set_xlim(self.xminlim, self.xmaxlim)
        a.set_ylim(self.yminlim, self.ymaxlim)

    def showFid(self):
        self.ax.cla()
        if self.spec == 1:
            if self.current.ppm:
                axMult = 1e6 / self.current.ref
            else:
                axMult = 1.0 / (1000.0**self.current.axType)
        elif self.spec == 0:
            axMult = 1000.0**self.current.axType
        self.line_xdata = self.xax * axMult
        self.line_ydata = self.data1D
        self.ax.plot(self.xax * axMult, self.data1D, c=self.current.color, marker=self.MARKER, linestyle=self.LINESTYLE, linewidth=self.current.linewidth, label=self.current.data.name, picker=True)
        if self.fitDataList[tuple(self.locList)] is not None:
            tmp = self.fitDataList[tuple(self.locList)]
            self.ax.plot(tmp[0] * axMult, tmp[1], picker=True)
            for i in range(len(tmp[2])):
                self.ax.plot(tmp[2][i] * axMult, tmp[3][i], picker=True)
        if self.spec == 0:
            if self.current.axType == 0:
                self.ax.set_xlabel('Time [s]')
            elif self.current.axType == 1:
                self.ax.set_xlabel('Time [ms]')
            elif self.current.axType == 2:
                self.ax.set_xlabel(r'Time [$\mu$s]')
            else:
                self.ax.set_xlabel('User defined')
        elif self.spec == 1:
            if self.current.ppm:
                self.ax.set_xlabel('Frequency [ppm]')
            else:
                if self.current.axType == 0:
                    self.ax.set_xlabel('Frequency [Hz]')
                elif self.current.axType == 1:
                    self.ax.set_xlabel('Frequency [kHz]')
                elif self.current.axType == 2:
                    self.ax.set_xlabel('Frequency [MHz]')
                else:
                    self.ax.set_xlabel('User defined')
        else:
            self.ax.set_xlabel('')
        self.ax.get_xaxis().get_major_formatter().set_powerlimits((-4, 4))
        self.ax.get_yaxis().get_major_formatter().set_powerlimits((-4, 4))
        if self.spec > 0:
            self.ax.set_xlim(self.xmaxlim, self.xminlim)
        else:
            self.ax.set_xlim(self.xminlim, self.xmaxlim)
        self.ax.set_ylim(self.yminlim, self.ymaxlim)
        self.canvas.draw()

#################################################################################


class AbstractParamFrame(QtWidgets.QWidget):

    TICKS = True # Fitting parameters can be fixed by checkboxes

    def __init__(self, parent, rootwindow, isMain=True):
        super(AbstractParamFrame, self).__init__(rootwindow)
        self.parent = parent
        self.FITNUM = self.parent.FITNUM
        self.rootwindow = rootwindow
        self.isMain = isMain # display fitting buttons
        tmp = list(self.parent.data.data.shape)
        tmp.pop(self.parent.axes)
        self.fitParamList = np.zeros(tmp, dtype=object)
        self.fitNumList = np.zeros(tmp, dtype=int)
        grid = QtWidgets.QGridLayout(self)
        self.setLayout(grid)
        if self.parent.current.spec == 1:
            self.axAdd = self.parent.current.freq - self.parent.current.ref
            if self.parent.current.ppm:
                self.axUnit = 'ppm'
                self.axMult = 1e6 / self.parent.current.ref
            else:
                self.axMult = 1.0 / (1000.0**self.parent.current.axType)
                axUnits = ['Hz','kHz','MHz']
                self.axUnit = axUnits[self.parent.current.axType]
        elif self.parent.current.spec == 0:
            axUnits = ['s','ms', u"\u03bcs"]
            self.axUnit = axUnits[self.parent.current.axType]
            self.axMult = 1000.0**self.parent.current.axType
            self.axAdd = 0
        self.frame1 = QtWidgets.QGridLayout()
        self.optframe = QtWidgets.QGridLayout()
        self.frame2 = QtWidgets.QGridLayout()
        self.frame3 = QtWidgets.QGridLayout()
        self.frame4 = QtWidgets.QGridLayout()
        grid.addLayout(self.frame1, 0, 0)
        grid.addLayout(self.optframe, 0, 1)
        grid.addLayout(self.frame2, 0, 2)
        grid.addLayout(self.frame3, 0, 3)
        grid.addLayout(self.frame4, 0, 4)
        grid.setColumnStretch(10, 1)
        grid.setAlignment(QtCore.Qt.AlignLeft)
        simButton = QtWidgets.QPushButton("Sim")
        simButton.clicked.connect(self.rootwindow.sim)
        self.frame1.addWidget(simButton, 0, 0)
        fitButton = QtWidgets.QPushButton("Fit")
        fitButton.clicked.connect(self.rootwindow.fit)
        self.frame1.addWidget(fitButton, 1, 0)
        self.stopButton = QtWidgets.QPushButton("Stop")
        self.stopButton.clicked.connect(self.stopMP)
        self.frame1.addWidget(self.stopButton, 1, 0)
        self.stopButton.hide()
        self.process1 = None
        self.queue = None
        fitAllButton = QtWidgets.QPushButton("Fit all")
        fitAllButton.clicked.connect(self.fitAll)
        self.frame1.addWidget(fitAllButton, 2, 0)
        self.stopAllButton = QtWidgets.QPushButton("Stop all")
        self.stopAllButton.clicked.connect(self.stopAll)
        self.frame1.addWidget(self.stopAllButton, 2, 0)
        self.stopAllButton.hide()
        copyParamsButton = QtWidgets.QPushButton("Copy parameters")
        copyParamsButton.clicked.connect(self.copyParams)
        self.frame1.addWidget(copyParamsButton, 3, 0)
        copyResultButton = QtWidgets.QPushButton("Result to workspace")
        copyResultButton.clicked.connect(self.resultToWorkspaceWindow)
        self.frame1.addWidget(copyResultButton, 4, 0)
        copyParamButton = QtWidgets.QPushButton("Param. to workspace")
        copyParamButton.clicked.connect(self.paramToWorkspaceWindow)
        self.frame1.addWidget(copyParamButton, 5, 0)
        addSpecButton = QtWidgets.QPushButton("Add spectrum")
        addSpecButton.clicked.connect(self.rootwindow.addSpectrum)
        self.frame1.addWidget(addSpecButton, 6, 0)
        if self.isMain:
            cancelButton = QtWidgets.QPushButton("&Cancel")
            cancelButton.clicked.connect(self.closeWindow)
        else:
            cancelButton = QtWidgets.QPushButton("&Delete")
            cancelButton.clicked.connect(self.rootwindow.removeSpectrum)
        prefButton = QtWidgets.QPushButton("Preferences")
        prefButton.clicked.connect(self.createPrefWindow)
        self.frame1.addWidget(prefButton, 0, 1)
        self.frame1.addWidget(cancelButton, 7, 0)
        self.frame1.setColumnStretch(10, 1)
        self.frame1.setAlignment(QtCore.Qt.AlignTop)
        self.checkFitParamList(tuple(self.parent.locList))

    def checkFitParamList(self, locList):
        if not self.fitParamList[locList]:
            self.fitParamList[locList] = self.defaultValues(0)
        
    def closeWindow(self, *args):
        self.stopMP()
        self.rootwindow.cancel()

    def copyParams(self):
        self.checkInputs()
        locList = tuple(self.parent.locList)
        self.checkFitParamList(locList)
        for elem in np.nditer(self.fitParamList, flags=["refs_ok"], op_flags=['readwrite']):
            elem[...] = copy.deepcopy(self.fitParamList[locList])
        for elem in np.nditer(self.fitNumList, flags=["refs_ok"], op_flags=['readwrite']):
            elem[...] = self.fitNumList[locList]

    def dispParams(self):
        locList = tuple(self.parent.locList)
        val = self.fitNumList[locList] + 1
        for name in self.SINGLENAMES:
            if isinstance(self.fitParamList[locList][name][0], (float, int)):
                self.entries[name][0].setText(('%#.'+str(self.rootwindow.tabWindow.PRECIS)+'g') % self.fitParamList[locList][name][0])
            if self.TICKS:
                self.ticks[name][0].setChecked(self.fitParamList[locList][name][1])
        self.setNumExp()
        for i in range(self.FITNUM):
            for name in self.MULTINAMES:
                if isinstance(self.fitParamList[locList][name][i][0], (float, int)):
                    self.entries[name][i].setText(('%#.'+str(self.rootwindow.tabWindow.PRECIS)+'g') % self.fitParamList[locList][name][i][0])
                if self.TICKS:
                    self.ticks[name][i].setChecked(self.fitParamList[locList][name][i][1])
                if i < val:
                    if self.TICKS:
                        self.ticks[name][i].show()
                    self.entries[name][i].show()
                else:
                    if self.TICKS:
                        self.ticks[name][i].hide()
                    self.entries[name][i].hide()

    def setNumExp(self):
        locList = tuple(self.parent.locList)
        self.numExp.setCurrentIndex(self.fitNumList[locList])
                    
    def changeNum(self, *args):
        val = self.numExp.currentIndex() + 1
        self.fitNumList[tuple(self.parent.locList)] = self.numExp.currentIndex()
        for i in range(self.FITNUM):
            for name in self.MULTINAMES:
                if i < val:
                    if self.TICKS:
                        self.ticks[name][i].show()
                    self.entries[name][i].show()
                else:
                    if self.TICKS:
                        self.ticks[name][i].hide()
                    self.entries[name][i].hide()

    def checkInputs(self):
        locList = tuple(self.parent.locList)
        numExp = self.getNumExp()
        for name in self.SINGLENAMES:
            if self.TICKS:
                self.fitParamList[locList][name][1] = self.ticks[name][0].isChecked()
            inp = safeEval(self.entries[name][0].text())
            if inp is None:
                return False
            elif isinstance(inp, float):
                self.entries[name][0].setText(('%#.'+str(self.rootwindow.tabWindow.PRECIS)+'g') % inp)
            else:
                self.entries[name][0].setText(str(inp))
            self.fitParamList[locList][name][0] = inp
        for i in range(numExp):
            for name in self.MULTINAMES:
                if self.TICKS:
                    self.fitParamList[locList][name][i][1] = self.ticks[name][i].isChecked()
                inp = safeEval(self.entries[name][i].text())
                if inp is None:
                    return False
                elif isinstance(inp, float):
                    self.entries[name][i].setText(('%#.'+str(self.rootwindow.tabWindow.PRECIS)+'g') % inp)
                else:
                    self.entries[name][i].setText(str(inp))
                self.fitParamList[locList][name][i][0] = inp
        return True

    def getNumExp(self):
        return self.numExp.currentIndex() + 1
    
    def getExtraParams(self, out):
        return (out, [])
    
    def getFitParams(self):
        if not self.checkInputs():
            self.rootwindow.mainProgram.dispMsg("One of the inputs is not valid")
            return
        struc = {}
        for name in (self.SINGLENAMES + self.MULTINAMES):
            struc[name] = []
        guess = []
        argu = []
        numExp = self.getNumExp()
        out = {}
        for name in self.SINGLENAMES:
            out[name] = [0.0]
        for name in self.MULTINAMES:
            out[name] = np.zeros(numExp)
        for name in self.SINGLENAMES:
            if isfloat(self.entries[name][0].text()):
                if not self.fitParamList[tuple(self.parent.locList)][name][1]:
                    guess.append(float(self.entries[name][0].text()))
                    struc[name].append((1, len(guess)-1))
                else:
                    out[name][0] = float(self.entries[name][0].text())
                    argu.append(out[name][0])
                    struc[name].append((0, len(argu)-1))
            else:
                struc[name].append((2, checkLinkTuple(safeEval(self.entries[name][0].text()))))
        for i in range(numExp):
            for name in self.MULTINAMES:
                if isfloat(self.entries[name][i].text()):
                    if not self.fitParamList[tuple(self.parent.locList)][name][i][1]:
                        guess.append(float(self.entries[name][i].text()))
                        struc[name].append((1, len(guess)-1))
                    else:
                        out[name][i] = float(self.entries[name][i].text())
                        argu.append(out[name][i])
                        struc[name].append((0, len(argu)-1))
                else:
                    struc[name].append((2, checkLinkTuple(safeEval(self.entries[name][i].text()))))
        out, extraArgu = self.getExtraParams(out)
        argu.append(extraArgu)
        args = ([numExp], [struc], [argu], [self.parent.current.sw], [self.axAdd], [self.axMult])
        return (self.parent.xax, self.parent.data1D, guess, args, out)

    def fit(self, xax, data1D, guess, args):
        self.queue = multiprocessing.Queue()
        self.process1 = multiprocessing.Process(target=self.FITFUNC, args=(xax, data1D, guess, args, self.queue, self.rootwindow.tabWindow.MINMETHOD))
        self.process1.start()
        self.running = True
        self.stopButton.show()
        while self.running:
            if not self.queue.empty():
                self.running = False
            QtWidgets.qApp.processEvents()
            time.sleep(0.1)
        if self.queue is None:
            return
        fitVal = self.queue.get(timeout=2)
        self.stopMP()
        if fitVal is None:
            self.rootwindow.mainProgram.dispMsg('Optimal parameters not found')
            return
        return fitVal

    def setResults(self, fitVal, args, out):
        locList = tuple(self.parent.locList)
        numExp = args[0][0]
        struc = args[1][0]
        for name in self.SINGLENAMES:
            if struc[name][0][0] == 1:
                self.fitParamList[locList][name][0] = fitVal[struc[name][0][1]]
        for i in range(numExp):
            for name in self.MULTINAMES:
                if struc[name][i][0] == 1:
                    self.fitParamList[locList][name][i][0] = fitVal[struc[name][i][1]]
        self.dispParams()
        self.rootwindow.sim()

    def stopMP(self, *args):
        if self.queue is not None:
            self.process1.terminate()
            self.queue.close()
            self.queue.join_thread()
            self.process1.join()
        self.queue = None
        self.process1 = None
        self.running = False
        self.stopButton.hide()

    def stopAll(self, *args):
        self.runningAll = False
        self.stopMP()
        self.stopAllButton.hide()

    def fitAll(self, *args):
        self.runningAll = True
        self.stopAllButton.show()
        tmp = list(self.parent.data.data.shape)
        tmp.pop(self.parent.axes)
        tmp2 = ()
        for i in tmp:
            tmp2 += (np.arange(i),)
        grid = np.array([i.flatten() for i in np.meshgrid(*tmp2)]).T
        for i in grid:
            QtWidgets.qApp.processEvents()
            if self.runningAll is False:
                break
            self.parent.setSlice(self.parent.axes, i)
            self.rootwindow.fit()
            self.rootwindow.fittingSideFrame.upd()
        self.stopAllButton.hide()

    def getSimParams(self):
        if not self.checkInputs():
            self.rootwindow.mainProgram.dispMsg("One of the inputs is not valid")
            return
        numExp = self.getNumExp()
        out = {}
        for name in self.SINGLENAMES:
            out[name] = [0.0]
        for name in self.MULTINAMES:
            out[name] = [0.0]*numExp
        for name in self.SINGLENAMES:
            inp = safeEval(self.entries[name][0].text())
            out[name][0] = inp
        for i in range(numExp):
            for name in self.MULTINAMES:
                inp = safeEval(self.entries[name][i].text())
                out[name][i] = inp
        out, tmp = self.getExtraParams(out)
        return out

    def paramToWorkspaceWindow(self):
        paramNameList = self.SINGLENAMES + self.MULTINAMES
        if self.parent.data.data.ndim == 1:
            single = True
        else:
            single = False
        ParamCopySettingsWindow(self, paramNameList, lambda allTraces, settings, self=self: self.paramToWorkspace(allTraces, settings), single)

    def paramToWorkspace(self, allTraces, settings):
        if not self.checkInputs():
            self.rootwindow.mainProgram.dispMsg("One of the inputs is not valid")
            return
        paramNameList = np.array(self.SINGLENAMES + self.MULTINAMES, dtype=object)
        locList = tuple(self.parent.locList)
        if not np.any(settings):
            return
        names = paramNameList[settings]
        if allTraces:
            maxNum = np.max(self.fitNumList)+1
            tmp = list(self.parent.data.data.shape)
            tmp.pop(self.parent.axes)
            data = np.zeros((sum(settings), maxNum) + tuple(tmp))
            tmp2 = ()
            for i in tmp:
                tmp2 += (np.arange(i),)
            grid = np.array([i.flatten() for i in np.meshgrid(*tmp2)]).T
            for i in grid:
                self.checkFitParamList(tuple(i))
                for j in range(len(names)):
                    if names[j] in self.SINGLENAMES:
                        data[(j,)+(slice(None),)+tuple(i)].fill(self.fitParamList[tuple(i)][names[j]][0])
                    else:
                        data[(j,)+(slice(None),)+tuple(i)] = self.fitParamList[tuple(i)][names[j]].T[0][:(self.fitNumList[tuple(i)]+1)]
            self.rootwindow.createNewData(data, self.parent.current.axes, True, True)
        else:
            data = np.zeros((sum(settings), self.fitNumList[locList]+1))
            for i in range(len(names)):
                if names[i] in self.SINGLENAMES:
                    data[i].fill(self.fitParamList[locList][names[i]][0])
                else:
                    data[i] = self.fitParamList[locList][names[i]].T[0][:(self.fitNumList[locList]+1)]
            self.rootwindow.createNewData(data, self.parent.current.axes, True)

    def resultToWorkspaceWindow(self):
        if self.parent.data.data.ndim == 1:
            single = True
        else:
            single = False
        FitCopySettingsWindow(self, lambda settings, self=self: self.resultToWorkspace(settings), single)

    def resultToWorkspace(self, settings):
        if settings is None:
            return
        if settings[0]:
            oldLocList = self.parent.locList
            maxNum = np.max(self.fitNumList)+1
            extraLength = 1
            if settings[1]:
                extraLength += 1
            if settings[2]:
                extraLength += maxNum
            if settings[3]:
                extraLength += 1
            data = np.zeros((extraLength,) + self.parent.data.data.shape)
            tmp = list(self.parent.data.data.shape)
            tmp.pop(self.parent.axes)
            tmp2 = ()
            for i in tmp:
                tmp2 += (np.arange(i),)
            grid = np.array([i.flatten() for i in np.meshgrid(*tmp2)]).T
            for i in grid:
                self.parent.setSlice(self.parent.axes, i)
                data[(slice(None),) + tuple(i[:self.parent.axes]) + (slice(None),) + tuple(i[self.parent.axes:])] = self.prepareResultToWorkspace(settings, maxNum)
            self.parent.setSlice(self.parent.axes, oldLocList)
            self.rootwindow.createNewData(data, self.parent.current.axes, False, True)
        else:
            data = self.prepareResultToWorkspace(settings)
            self.rootwindow.createNewData(data, self.parent.current.axes, False)

    def prepareResultToWorkspace(self, settings, minLength=1):
        self.calculateResultsToWorkspace(True)
        fitData = self.parent.fitDataList[tuple(self.parent.locList)]
        if fitData is None:
            fitData = [np.zeros(len(self.parent.data1D)), np.zeros(len(self.parent.data1D)), np.zeros(len(self.parent.data1D)), np.array([np.zeros(len(self.parent.data1D))]*minLength)]
        outCurvePart = []
        if settings[1]:
            outCurvePart.append(self.parent.data1D)
        if settings[2]:
            for i in fitData[3]:
                outCurvePart.append(i)
            if len(fitData[3]) < minLength:
                for i in range(minLength-len(fitData[3])):
                    outCurvePart.append(np.zeros(len(self.parent.data1D)))
        if settings[3]:
            outCurvePart.append(self.parent.data1D-fitData[1])
        outCurvePart.append(fitData[1])
        self.calculateResultsToWorkspace(False)
        return np.array(outCurvePart)

    def calculateResultsToWorkspace(self, *args):
        # Some fitting methods need to recalculate the curves before exporting
        pass

    def createPrefWindow(self, *args):
        PrefWindow(self.rootwindow.tabWindow)

##############################################################################


class PrefWindow(QtWidgets.QWidget):

    METHODLIST = ['Powell', 'Nelder-Mead']

    def __init__(self, parent):
        super(PrefWindow, self).__init__(parent)
        self.setWindowFlags(QtCore.Qt.Window | QtCore.Qt.Tool)
        self.father = parent
        self.setWindowTitle("Preferences")
        layout = QtWidgets.QGridLayout(self)
        grid = QtWidgets.QGridLayout()
        layout.addLayout(grid, 0, 0, 1, 2)
        grid.addWidget(QLabel("Min. method:"), 0, 0)
        self.minmethodBox = QtWidgets.QComboBox(self)
        self.minmethodBox.addItems(self.METHODLIST)
        self.minmethodBox.setCurrentIndex(self.METHODLIST.index(self.father.MINMETHOD))
        grid.addWidget(self.minmethodBox, 0, 1)
        grid.addWidget(QLabel("Significant digits:"), 1, 0)
        self.precisBox = QtWidgets.QSpinBox(self)
        self.precisBox.setValue(self.father.PRECIS)
        grid.addWidget(self.precisBox, 1, 1)
        cancelButton = QtWidgets.QPushButton("&Cancel")
        cancelButton.clicked.connect(self.closeEvent)
        layout.addWidget(cancelButton, 2, 0)
        okButton = QtWidgets.QPushButton("&Ok", self)
        okButton.clicked.connect(self.applyAndClose)
        okButton.setFocus()
        layout.addWidget(okButton, 2, 1)
        grid.setRowStretch(100, 1)
        self.show()
        self.setGeometry(self.frameSize().width() - self.geometry().width(), self.frameSize().height() - self.geometry().height(), 0, 0)

    def closeEvent(self, *args):
        self.deleteLater()

    def applyAndClose(self, *args):
        self.father.PRECIS = self.precisBox.value()
        self.father.MINMETHOD = self.METHODLIST[self.minmethodBox.currentIndex()]
        self.closeEvent()

##############################################################################

    
class IntegralsWindow(TabFittingWindow):

    def __init__(self, mainProgram, oldMainWindow):
        self.CURRENTWINDOW = IntegralsFrame
        self.PARAMFRAME = IntegralsParamFrame
        super(IntegralsWindow, self).__init__(mainProgram, oldMainWindow)

#################################################################################


class IntegralsFrame(FitPlotFrame):

    def __init__(self, rootwindow, fig, canvas, current):
        self.FITNUM = 20 # Maximum number of fits
        self.pickNum = 0
        self.pickWidth = False
        super(IntegralsFrame, self).__init__(rootwindow, fig, canvas, current)

    def togglePick(self, var):
        self.peakPickReset()
        if var == 1:
            self.peakPickFunc = lambda pos, self=self: self.pickDeconv(pos)
            self.peakPick = True
        else:
            self.peakPickFunc = None
            self.peakPick = False

    def pickDeconv(self, pos):
        pickNum = self.fitPickNumList[tuple(self.locList)]
        if self.pickWidth:
            self.rootwindow.paramframe.entries['max'][pickNum].setText(('%#.'+str(self.rootwindow.tabWindow.PRECIS)+'g') % pos[1])
            self.fitPickNumList[tuple(self.locList)] += 1
            self.pickWidth = False
            self.rootwindow.fit()
        else:
            self.rootwindow.paramframe.entries['min'][pickNum].setText(('%#.'+str(self.rootwindow.tabWindow.PRECIS)+'g') % pos[1])
            if pickNum < self.FITNUM:
                self.rootwindow.paramframe.numExp.setCurrentIndex(pickNum)
                self.rootwindow.paramframe.changeNum()
            self.pickWidth = True
        if pickNum < self.FITNUM:
            self.peakPickFunc = lambda pos, self=self: self.pickDeconv(pos)
            self.peakPick = True

#################################################################################


class IntegralsParamFrame(AbstractParamFrame):

    TICKS = False
    
    def __init__(self, parent, rootwindow, isMain=True):
        self.SINGLENAMES = []
        self.MULTINAMES = ['min', 'max', 'amp']
        self.FITFUNC = integralsmpFit
        super(IntegralsParamFrame, self).__init__(parent, rootwindow, isMain)
        resetButton = QtWidgets.QPushButton("Reset")
        resetButton.clicked.connect(self.reset)
        self.frame1.addWidget(resetButton, 1, 1)
        self.pickTick = QtWidgets.QCheckBox("Pick")
        self.pickTick.stateChanged.connect(self.togglePick)
        self.frame1.addWidget(self.pickTick, 2, 1)
        self.absIntTick = QtWidgets.QCheckBox("Relative integrals")
        self.absIntTick.setChecked(True)
        self.absIntTick.stateChanged.connect(self.rootwindow.fit)
        self.optframe.addWidget(self.absIntTick, 0, 0)
        self.optframe.setColumnStretch(10, 1)
        self.optframe.setAlignment(QtCore.Qt.AlignTop)
        self.numExp = QtWidgets.QComboBox()
        self.numExp.addItems([str(x+1) for x in range(self.FITNUM)])
        self.numExp.currentIndexChanged.connect(self.changeNum)
        self.frame3.addWidget(self.numExp, 0, 0)
        self.frame3.addWidget(QLabel("Min bounds:"), 1, 0)
        self.frame3.addWidget(QLabel("Max bounds:"), 1, 1)
        self.frame3.addWidget(QLabel("Integral:"), 1, 2)
        self.frame3.setColumnStretch(20, 1)
        self.frame3.setAlignment(QtCore.Qt.AlignTop)
        self.refVal = None
        self.entries = {'min':[], 'max':[], 'amp':[]}
        if self.parent.current.plotType == 0:
            self.maxy = np.amax(np.real(self.parent.current.data1D))
            self.diffy = self.maxy - np.amin(np.real(self.parent.current.data1D))
        elif self.parent.current.plotType == 1:
            self.maxy = np.amax(np.imag(self.parent.current.data1D))
            self.diffy = self.maxy - np.amin(np.imag(self.parent.current.data1D))
        elif self.parent.current.plotType == 2:
            self.maxy = np.amax(np.real(self.parent.current.data1D))
            self.diffy = self.maxy - np.amin(np.real(self.parent.current.data1D))
        elif self.parent.current.plotType == 3:
            self.maxy = np.amax(np.abs(self.parent.current.data1D))
            self.diffy = self.maxy - np.amin(np.abs(self.parent.current.data1D))
        for i in range(self.FITNUM):
            for j in range(len(self.MULTINAMES)):
                self.entries[self.MULTINAMES[j]].append(QtWidgets.QLineEdit())
                self.entries[self.MULTINAMES[j]][i].setAlignment(QtCore.Qt.AlignHCenter)
                self.frame3.addWidget(self.entries[self.MULTINAMES[j]][i], i + 2, j)
        self.resetVals()

    def defaultValues(self, inp):
        if not inp:
            return {'min':np.repeat([np.array([min(self.parent.current.xax), True], dtype=object)], self.FITNUM, axis=0), 'max':np.repeat([np.array([max(self.parent.current.xax), True], dtype=object)], self.FITNUM,axis=0), 'amp':np.repeat([np.array([1.0, False], dtype=object)], self.FITNUM,axis=0)}
        else:
            return inp

    def reset(self):
        self.resetVals()
        self.rootwindow.fit()
        
    def resetVals(self):
        locList = tuple(self.parent.locList)
        self.refVal = None
        self.fitNumList[locList] = 0
        self.pickTick.setChecked(True)
        defaults = {'min':[min(self.parent.current.xax),True], 'max':[max(self.parent.current.xax),True], 'amp':[1.0,False]}
        for i in range(self.FITNUM):
            for name in defaults.keys():
                self.fitParamList[locList][name][i] = defaults[name]
        self.togglePick()
        self.parent.pickWidth = False
        self.parent.fitPickNumList[locList] = 0
        self.dispParams()

    def togglePick(self):
        self.parent.togglePick(self.pickTick.isChecked())

    def setVal(self, entry, isMin=False):
        locList = tuple(self.parent.locList)
        inp = safeEval(entry.text())
        if inp is None:
            self.rootwindow.mainProgram.dispMsg("One of the inputs is not valid")
            return
        if inp < min(self.parent.current.xax):
            inp = min(self.parent.current.xax)
        if inp > max(self.parent.current.xax):
            inp = max(self.parent.current.xax)
        if isMin:
            num = self.entries['min'].index(entry)
            self.fitParamList[locList]['min'][num][0] = min(inp, self.fitParamList[locList]['max'][num][0])
            self.fitParamList[locList]['max'][num][0] = max(inp, self.fitParamList[locList]['max'][num][0])
        else:
            num = self.entries['max'].index(entry)
            self.fitParamList[locList]['min'][num][0] = max(inp, self.fitParamList[locList]['min'][num][0])
            self.fitParamList[locList]['max'][num][0] = min(inp, self.fitParamList[locList]['min'][num][0])
        self.fitNumList[tuple(self.parent.locList)] += 1
        self.first = True
        self.entries['min'][num].setText(('%#.'+str(self.rootwindow.tabWindow.PRECIS)+'g') % self.fitParamList[locList]['min'][num][0])
        self.entries['max'][num].setText(('%#.'+str(self.rootwindow.tabWindow.PRECIS)+'g') % self.fitParamList[locList]['max'][num][0])
        self.rootwindow.fit()

    def setRef(self, entry):
        locList = tuple(self.parent.locList)
        num = self.entries['amp'].index(entry)
        inp = safeEval(entry.text())
        if inp is None:
            self.rootwindow.mainProgram.dispMsg("One of the inputs is not valid")
            return
        self.refVal = self.fitParamList[locList]['amp'][num][0] / float(inp)
        self.rootwindow.fit()

    def displayInt(self):
        locList = tuple(self.parent.locList)
        if self.refVal is None:
            self.refVal = self.fitParamList[locList]['amp'][0][0]
        if self.absIntTick.isChecked():
            tmpInts = np.array([num[0] for num in self.fitParamList[locList]['amp']]) / float(self.refVal)
        else:
            tmpInts = np.array([num[0] for num in self.fitParamList[locList]['amp']])
        for i in range(self.FITNUM):
            self.entries['amp'][i].setText(('%#.'+str(self.rootwindow.tabWindow.PRECIS)+'g') % tmpInts[i])

    def disp(self, params, num, display=True, prepExport=False):
        out = params[num]
        numExp = len(out['min'])
        tmpx = self.parent.xax * self.axMult
        x = []
        outCurvePart = []
        maxDiff = 0.0
        for i in range(numExp):
            for name in ['min', 'max']:
                inp = out[name][i]
                if isinstance(inp, tuple):
                    inp = checkLinkTuple(inp)
                    out[name][i] = inp[2]*params[inp[4]][inp[0]][inp[1]] + inp[3]
                if not np.isfinite(out[name][i]):
                    self.rootwindow.mainProgram.dispMsg("One of the inputs is not valid")
                    return
            minVal = min(out['min'][i], out['max'][i])
            maxVal = max(out['min'][i], out['max'][i])
            bArray = (minVal < tmpx) & (maxVal > tmpx)
            if prepExport:
                x.append(self.parent.xax)
            else:
                x.append(self.parent.xax[bArray])
            if self.parent.spec:
                integral = np.cumsum(self.parent.data1D[bArray][::-1])[::-1]
            else:
                integral = np.cumsum(self.parent.data1D[bArray])
            if prepExport:
                tmpIntegral = np.zeros(len(self.parent.xax))
                tmpIntegral[bArray] = integral
                outCurvePart.append(tmpIntegral)
            else:
                outCurvePart.append(integral)
            maxDiff = max(maxDiff, max(outCurvePart[-1])-min(outCurvePart[-1]))
        self.displayInt()
        for i in range(len(outCurvePart)):
            outCurvePart[i] = outCurvePart[i] / maxDiff * self.diffy
        if prepExport:
            self.parent.fitDataList[tuple(self.parent.locList)] = [self.parent.xax, np.zeros(len(self.parent.xax)), x, outCurvePart]
        else:
            self.parent.fitDataList[tuple(self.parent.locList)] = [np.array([]), np.array([]), x, outCurvePart]
        if display:
            self.parent.showFid()

    def calculateResultsToWorkspace(self, prepExport):
        self.rootwindow.sim(display=False, prepExport=prepExport)


##############################################################################

def integralsmpFit(allX, data1D, guess, args, queue, minmethod):
    specName = args[0]
    specSlices = args[1]
    allStruc = args[3]
    allArgu = args[4]
    results = []
    for n in range(len(allX)):
        x=allX[n]
        data = data1D[:len(x)]
        data1D = np.delete(data1D, range(len(x)))
        testFunc = np.zeros(len(x))
        numExp = args[2][n]
        struc = args[3][n]
        argu = args[4][n]
        sw = args[5][n]
        axAdd = args[6][n]
        axMult = args[7][n]
        x = x*axMult
        parameters = {'min':0.0, 'max':0.0}
        for i in range(numExp):
            for name in ['min', 'max']:
                if struc[name][i][0] == 0:
                    parameters[name] = argu[struc[name][i][1]]
                else:
                    altStruc = struc[name][i][1]
                    strucTarget = allStruc[altStruc[4]]
                    parameters[name] = altStruc[2] * allArgu[altStruc[4]][strucTarget[altStruc[0]][altStruc[1]][1]] + altStruc[3]
            minVal = min(parameters['min'], parameters['max'])
            maxVal = max(parameters['min'], parameters['max'])
            tmpx = x[(minVal < x) & (maxVal > x)]
            tmpy = data[(minVal < x) & (maxVal > x)]
            results = np.append(results, np.sum(tmpy) * sw / float(len(data)))
    queue.put({'x':results})

##############################################################################


class RelaxWindow(TabFittingWindow):

    def __init__(self, mainProgram, oldMainWindow):
        self.CURRENTWINDOW = RelaxFrame
        self.PARAMFRAME = RelaxParamFrame
        super(RelaxWindow, self).__init__(mainProgram, oldMainWindow)
        
#################################################################################


class RelaxFrame(FitPlotFrame):

    MARKER = 'o'
    LINESTYLE = 'none'
    
    def __init__(self, rootwindow, fig, canvas, current):
        self.FITNUM = 4 # Maximum number of fits
        self.logx = 0
        self.logy = 0
        super(RelaxFrame, self).__init__(rootwindow, fig, canvas, current)

    def showFid(self):
        super(RelaxFrame, self).showFid()
        if self.logx == 0:
            self.ax.set_xscale('linear')
        else:
            self.ax.set_xscale('log')
        if self.logy == 0:
            self.ax.set_yscale('linear')
        else:
            self.ax.set_yscale('log')
        if self.logx == 0:
            self.ax.get_xaxis().get_major_formatter().set_powerlimits((-4, 4))
        if self.logy == 0:
            self.ax.get_yaxis().get_major_formatter().set_powerlimits((-4, 4))
        self.canvas.draw()

    def scroll(self, event):
        if self.rightMouse:
            if self.logx == 0:
                middle = (self.xmaxlim + self.xminlim) / 2.0
                width = self.xmaxlim - self.xminlim
                width = width * 0.9**event.step
                self.xmaxlim = middle + width / 2.0
                self.xminlim = middle - width / 2.0
                if self.spec > 0 and not isinstance(self, spectrum_classes.CurrentArrayed):
                    self.ax.set_xlim(self.xmaxlim, self.xminlim)
                else:
                    self.ax.set_xlim(self.xminlim, self.xmaxlim)
            else:
                middle = (np.log(self.xmaxlim) + np.log(self.xminlim)) / 2.0
                width = np.log(self.xmaxlim) - np.log(self.xminlim)
                width = width * 0.9**event.step
                self.xmaxlim = np.exp(middle + width / 2.0)
                self.xminlim = np.exp(middle - width / 2.0)
                if self.spec > 0 and not isinstance(self, spectrum_classes.CurrentArrayed):
                    self.ax.set_xlim(self.xmaxlim, self.xminlim)
                else:
                    self.ax.set_xlim(self.xminlim, self.xmaxlim)
        else:
            if self.logy == 0:
                middle = (self.ymaxlim + self.yminlim) / 2.0
                width = self.ymaxlim - self.yminlim
                width = width * 0.9**event.step
                self.ymaxlim = middle + width / 2.0
                self.yminlim = middle - width / 2.0
                if self.spec2 > 0 and isinstance(self, spectrum_classes.CurrentContour):
                    self.ax.set_ylim(self.ymaxlim, self.yminlim)
                else:
                    self.ax.set_ylim(self.yminlim, self.ymaxlim)
            else:
                middle = (np.log(self.ymaxlim) + np.log(self.yminlim)) / 2.0
                width = np.log(self.ymaxlim) - np.log(self.yminlim)
                width = width * 0.9**event.step
                self.ymaxlim = np.exp(middle + width / 2.0)
                self.yminlim = np.exp(middle - width / 2.0)
                if self.spec2 > 0 and isinstance(self, spectrum_classes.CurrentContour):
                    self.ax.set_ylim(self.ymaxlim, self.yminlim)
                else:
                    self.ax.set_ylim(self.yminlim, self.ymaxlim)
        self.canvas.draw()

    def buttonRelease(self, event):
        if event.button == 1:
            if self.peakPick:
                if self.rect[0] is not None:
                    self.rect[0].remove()
                    self.rect[0] = None
                    self.peakPick = False
                    idx = np.argmin(np.abs(self.line_xdata - event.xdata))
                    if self.peakPickFunc is not None:
                        self.peakPickFunc((idx, self.line_xdata[idx], self.line_ydata[idx]))
                    if not self.peakPick:  # check if peakpicking is still required
                        self.peakPickFunc = None
            else:
                self.leftMouse = False
                if self.rect[0] is not None:
                    self.rect[0].remove()
                if self.rect[1] is not None:
                    self.rect[1].remove()
                if self.rect[2] is not None:
                    self.rect[2].remove()
                if self.rect[3] is not None:
                    self.rect[3].remove()
                self.rect = [None, None, None, None]
                if self.zoomX2 is not None and self.zoomY2 is not None:
                    self.xminlim = min([self.zoomX1, self.zoomX2])
                    self.xmaxlim = max([self.zoomX1, self.zoomX2])
                    self.yminlim = min([self.zoomY1, self.zoomY2])
                    self.ymaxlim = max([self.zoomY1, self.zoomY2])
                    if self.spec > 0 and not isinstance(self, spectrum_classes.CurrentArrayed):
                        self.ax.set_xlim(self.xmaxlim, self.xminlim)
                    else:
                        self.ax.set_xlim(self.xminlim, self.xmaxlim)
                    if self.spec2 > 0 and isinstance(self, spectrum_classes.CurrentContour):
                        self.ax.set_ylim(self.ymaxlim, self.yminlim)
                    else:
                        self.ax.set_ylim(self.yminlim, self.ymaxlim)
                self.zoomX1 = None
                self.zoomX2 = None
                self.zoomY1 = None
                self.zoomY2 = None
        elif event.button == 3:
            self.rightMouse = False
        self.canvas.draw()

    def pan(self, event):
        if self.rightMouse and self.panX is not None and self.panY is not None:
            if self.logx == 0 and self.logy == 0:
                inv = self.ax.transData.inverted()
                point = inv.transform((event.x, event.y))
                x = point[0]
                y = point[1]
            else:
                x = event.xdata
                y = event.ydata
                if x is None or y is None:
                    return
            if self.logx == 0:
                diffx = x - self.panX
                self.xmaxlim = self.xmaxlim - diffx
                self.xminlim = self.xminlim - diffx
            else:
                diffx = np.log(x) - np.log(self.panX)
                self.xmaxlim = np.exp(np.log(self.xmaxlim) - diffx)
                self.xminlim = np.exp(np.log(self.xminlim) - diffx)
            if self.logy == 0:
                diffy = y - self.panY
                self.ymaxlim = self.ymaxlim - diffy
                self.yminlim = self.yminlim - diffy
            else:
                diffy = np.log(y) - np.log(self.panY)
                self.ymaxlim = np.exp(np.log(self.ymaxlim) - diffy)
                self.yminlim = np.exp(np.log(self.yminlim) - diffy)
            if self.spec > 0 and not isinstance(self, spectrum_classes.CurrentArrayed):
                self.ax.set_xlim(self.xmaxlim, self.xminlim)
            else:
                self.ax.set_xlim(self.xminlim, self.xmaxlim)
            if self.spec2 > 0 and isinstance(self, spectrum_classes.CurrentContour):
                self.ax.set_ylim(self.ymaxlim, self.yminlim)
            else:
                self.ax.set_ylim(self.yminlim, self.ymaxlim)
            self.canvas.draw()
        elif self.peakPick:
            if self.rect[0] is not None:
                self.rect[0].remove()
                self.rect[0] = None
            if event.xdata is not None:
                self.rect[0] = self.ax.axvline(event.xdata, c='k', linestyle='--')
            self.canvas.draw()
        elif self.leftMouse and (self.zoomX1 is not None) and (self.zoomY1 is not None):
            if self.logx == 0 and self.logy == 0:
                inv = self.ax.transData.inverted()
                point = inv.transform((event.x, event.y))
                self.zoomX2 = point[0]
                self.zoomY2 = point[1]
            else:
                self.zoomX2 = event.xdata
                self.zoomY2 = event.ydata
                if self.zoomX2 is None or self.zoomY2 is None:
                    return
            if self.rect[0] is not None:
                if self.rect[0] is not None:
                    self.rect[0].remove()
                if self.rect[1] is not None:
                    self.rect[1].remove()
                if self.rect[2] is not None:
                    self.rect[2].remove()
                if self.rect[3] is not None:
                    self.rect[3].remove()
                self.rect = [None, None, None, None]
            self.rect[0], = self.ax.plot([self.zoomX1, self.zoomX2], [self.zoomY2, self.zoomY2], 'k', clip_on=False)
            self.rect[1], = self.ax.plot([self.zoomX1, self.zoomX2], [self.zoomY1, self.zoomY1], 'k', clip_on=False)
            self.rect[2], = self.ax.plot([self.zoomX1, self.zoomX1], [self.zoomY1, self.zoomY2], 'k', clip_on=False)
            self.rect[3], = self.ax.plot([self.zoomX2, self.zoomX2], [self.zoomY1, self.zoomY2], 'k', clip_on=False)
            self.canvas.draw()

    def setLog(self, logx, logy):
        self.logx = logx
        self.logy = logy
        self.ax.set_xlim(self.xminlim, self.xmaxlim)
        self.ax.set_ylim(self.yminlim, self.ymaxlim)

#################################################################################


class RelaxParamFrame(AbstractParamFrame):

    def __init__(self, parent, rootwindow, isMain=True):
        self.SINGLENAMES = ['amp', 'const']
        self.MULTINAMES = ['coeff', 't']
        self.FITFUNC = relaxationmpFit
        super(RelaxParamFrame, self).__init__(parent, rootwindow, isMain)
        locList = tuple(self.parent.locList)
        self.ticks = {'amp':[], 'const':[], 'coeff':[], 't':[]}
        self.entries = {'amp':[], 'const':[], 'coeff':[], 't':[]}
        self.frame2.addWidget(QLabel("Amplitude:"), 0, 0, 1, 2)
        self.ticks['amp'].append(QtWidgets.QCheckBox(''))
        self.frame2.addWidget(self.ticks['amp'][-1], 1, 0)
        self.entries['amp'].append(QtWidgets.QLineEdit())
        self.entries['amp'][-1].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['amp'][-1].setText(('%#.'+str(self.rootwindow.tabWindow.PRECIS)+'g') % self.fitParamList[locList]['amp'][0])
        self.frame2.addWidget(self.entries['amp'][-1], 1, 1)
        self.frame2.addWidget(QLabel("Constant:"), 2, 0, 1, 2)
        self.ticks['const'].append(QtWidgets.QCheckBox(''))
        self.frame2.addWidget(self.ticks['const'][-1], 3, 0)
        self.entries['const'].append(QtWidgets.QLineEdit())
        self.entries['const'][-1].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['const'][-1].setText(('%#.'+str(self.rootwindow.tabWindow.PRECIS)+'g') % self.fitParamList[locList]['const'][0])
        self.frame2.addWidget(self.entries['const'][-1], 3, 1)
        self.frame2.setColumnStretch(10, 1)
        self.frame2.setAlignment(QtCore.Qt.AlignTop)
        self.numExp = QtWidgets.QComboBox()
        self.numExp.addItems(['1', '2', '3', '4'])
        self.numExp.currentIndexChanged.connect(self.changeNum)
        self.frame3.addWidget(self.numExp, 0, 0, 1, 2)
        self.frame3.addWidget(QLabel("Coefficient:"), 1, 0, 1, 2)
        self.frame3.addWidget(QLabel("T [s]:"), 1, 2, 1, 2)
        self.frame3.setColumnStretch(10, 1)
        self.frame3.setAlignment(QtCore.Qt.AlignTop)
        self.xlog = QtWidgets.QCheckBox('x-log')
        self.xlog.stateChanged.connect(self.setLog)
        self.optframe.addWidget(self.xlog, 0, 0, QtCore.Qt.AlignTop)
        self.ylog = QtWidgets.QCheckBox('y-log')
        self.ylog.stateChanged.connect(self.setLog)
        self.optframe.addWidget(self.ylog, 1, 0, QtCore.Qt.AlignTop)
        self.optframe.setColumnStretch(10, 1)
        self.optframe.setAlignment(QtCore.Qt.AlignTop)
        for i in range(self.FITNUM):
            for j in range(len(self.MULTINAMES)):
                self.ticks[self.MULTINAMES[j]].append(QtWidgets.QCheckBox(''))
                self.ticks[self.MULTINAMES[j]][i].setChecked(self.fitParamList[locList][self.MULTINAMES[j]][i][1])
                self.frame3.addWidget(self.ticks[self.MULTINAMES[j]][i], i + 2, 2*j)
                self.entries[self.MULTINAMES[j]].append(QtWidgets.QLineEdit())
                self.entries[self.MULTINAMES[j]][i].setAlignment(QtCore.Qt.AlignHCenter)
                self.entries[self.MULTINAMES[j]][i].setText(('%#.'+str(self.rootwindow.tabWindow.PRECIS)+'g') % self.fitParamList[locList][self.MULTINAMES[j]][i][0])
                self.frame3.addWidget(self.entries[self.MULTINAMES[j]][i], i + 2, 2*j+1)
                if i > 0:
                    self.ticks[self.MULTINAMES[j]][i].hide()
                    self.entries[self.MULTINAMES[j]][i].hide()

    def defaultValues(self, inp):
        if not inp:
            return {'amp':[np.amax(self.parent.data1D), False], 'const':[1.0, False], 'coeff':np.repeat([np.array([-1.0, False], dtype=object)], self.FITNUM, axis=0), 't':np.repeat([np.array([1.0, False], dtype=object)], self.FITNUM,axis=0)}
        else:
            return inp

    def setLog(self, *args):
        self.parent.setLog(self.xlog.isChecked(), self.ylog.isChecked())
        self.sim()

    def disp(self, params, num, display=True, prepExport=False):
        out = params[num]
        for name in ['amp', 'const']:
            inp = out[name][0]
            if isinstance(inp, tuple):
                inp = checkLinkTuple(inp)
                out[name][0] = inp[2]*params[inp[4]][inp[0]][inp[1]] + inp[3]
        numExp = len(out['coeff'])
        for i in range(numExp):
            for name in ['coeff', 't']:
                inp = out[name][i]
                if isinstance(inp, tuple):
                    inp = checkLinkTuple(inp)
                    out[name][i] = inp[2]*params[inp[4]][inp[0]][inp[1]] + inp[3]
                if not np.isfinite(out[name][i]):
                    self.rootwindow.mainProgram.dispMsg("One of the inputs is not valid")
                    return
        tmpx = self.parent.xax
        if prepExport:
            x = tmpx
            outCurve = out['const'][0]*np.ones(len(x))
        else:
            numCurve = 100  # number of points in output curve
            outCurve = out['const'][0]*np.ones(numCurve)
            if self.xlog.isChecked():
                x = np.logspace(np.log(min(tmpx)), np.log(max(tmpx)), numCurve)
            else:
                x = np.linspace(min(tmpx), max(tmpx), numCurve)
        for i in range(len(out['coeff'])):
            outCurve += out['coeff'][i] * np.exp(-x / out['t'][i])
        self.parent.fitDataList[tuple(self.parent.locList)] = [x, out['amp'][0]*outCurve, [], []]
        if display:
            self.parent.showFid()

    def calculateResultsToWorkspace(self, prepExport):
        self.rootwindow.sim(display=False, prepExport=prepExport)

#############################################################################

def relaxationmpFit(xax, data1D, guess, args, queue, minmethod):
    try:
        fitVal = scipy.optimize.minimize(lambda *param: np.sum((data1D-relaxationfitFunc(param, xax, args))**2), guess, method=minmethod)
    except:
        fitVal = None
    queue.put(fitVal)

def relaxationfitFunc(params, allX, args):
    params = params[0]
    specName = args[0]
    specSlices = args[1]
    allParam = []
    for length in specSlices:
        allParam.append(params[length])
    allStruc = args[3]
    allArgu = args[4]
    fullTestFunc = []
    for n in range(len(allX)):
        x=allX[n]
        testFunc = np.zeros(len(x))
        param = allParam[n]
        numExp = args[2][n]
        struc = args[3][n]
        argu = args[4][n]
        sw = args[5][n]
        axAdd = args[6][n]
        axMult = args[7][n]
        parameters = {'amp':0.0, 'const':0.0, 'coeff':0.0, 't':0.0}
        for name in ['amp', 'const']:
            if struc[name][0][0] == 1:
                parameters[name] = param[struc[name][0][1]]
            elif struc[name][0][0] == 0:
                parameters[name] = argu[struc[name][0][1]]
            else:
                altStruc = struc[name][0][1]
                if struc[altStruc[0]][altStruc[1]][0] == 1:
                    parameters[name] = altStruc[2] * allParam[altStruc[4]][struc[altStruc[0]][altStruc[1]][1]] + altStruc[3]
                elif struc[altStruc[0]][altStruc[1]][0] == 0:
                    parameters[name] = altStruc[2] * allArgu[altStruc[4]][struc[altStruc[0]][altStruc[1]][1]] + altStruc[3]
        for i in range(numExp):
            for name in ['coeff', 't']:
                if struc[name][i][0] == 1:
                    parameters[name] = param[struc[name][i][1]]
                elif struc[name][i][0] == 0:
                    parameters[name] = argu[struc[name][i][1]]
                else:
                    altStruc = struc[name][i][1]
                    strucTarget = allStruc[altStruc[4]]
                    if strucTarget[altStruc[0]][altStruc[1]][0] == 1:
                        parameters[name] = altStruc[2] * allParam[altStruc[4]][strucTarget[altStruc[0]][altStruc[1]][1]] + altStruc[3]
                    elif strucTarget[altStruc[0]][altStruc[1]][0] == 0:
                        parameters[name] = altStruc[2] * allArgu[altStruc[4]][strucTarget[altStruc[0]][altStruc[1]][1]] + altStruc[3]
            testFunc += parameters['coeff'] * np.exp(-1.0 * x / parameters['t'])
        fullTestFunc = np.append(fullTestFunc, parameters['amp'] * (parameters['const'] + testFunc))
    return fullTestFunc

##############################################################################


class DiffusionWindow(TabFittingWindow):

    def __init__(self, mainProgram, oldMainWindow):
        self.CURRENTWINDOW = DiffusionFrame
        self.PARAMFRAME = DiffusionParamFrame
        super(DiffusionWindow, self).__init__(mainProgram, oldMainWindow)

#################################################################################


class DiffusionFrame(FitPlotFrame):

    MARKER = 'o'
    LINESTYLE = 'none'
    
    def __init__(self, rootwindow, fig, canvas, current):
        self.FITNUM = 4 # Maximum number of fits
        self.logx = 0
        self.logy = 0
        super(DiffusionFrame, self).__init__(rootwindow, fig, canvas, current)

    def showFid(self):
        super(DiffusionFrame, self).showFid()
        if self.logx == 0:
            self.ax.set_xscale('linear')
        else:
            self.ax.set_xscale('log')
        if self.logy == 0:
            self.ax.set_yscale('linear')
        else:
            self.ax.set_yscale('log')
        if self.logx == 0:
            self.ax.get_xaxis().get_major_formatter().set_powerlimits((-4, 4))
        if self.logy == 0:
            self.ax.get_yaxis().get_major_formatter().set_powerlimits((-4, 4))
        self.canvas.draw()

    def scroll(self, event):
        if self.rightMouse:
            if self.logx == 0:
                middle = (self.xmaxlim + self.xminlim) / 2.0
                width = self.xmaxlim - self.xminlim
                width = width * 0.9**event.step
                self.xmaxlim = middle + width / 2.0
                self.xminlim = middle - width / 2.0
                if self.spec > 0 and not isinstance(self, spectrum_classes.CurrentArrayed):
                    self.ax.set_xlim(self.xmaxlim, self.xminlim)
                else:
                    self.ax.set_xlim(self.xminlim, self.xmaxlim)
            else:
                middle = (np.log(self.xmaxlim) + np.log(self.xminlim)) / 2.0
                width = np.log(self.xmaxlim) - np.log(self.xminlim)
                width = width * 0.9**event.step
                self.xmaxlim = np.exp(middle + width / 2.0)
                self.xminlim = np.exp(middle - width / 2.0)
                if self.spec > 0 and not isinstance(self, spectrum_classes.CurrentArrayed):
                    self.ax.set_xlim(self.xmaxlim, self.xminlim)
                else:
                    self.ax.set_xlim(self.xminlim, self.xmaxlim)
        else:
            if self.logy == 0:
                middle = (self.ymaxlim + self.yminlim) / 2.0
                width = self.ymaxlim - self.yminlim
                width = width * 0.9**event.step
                self.ymaxlim = middle + width / 2.0
                self.yminlim = middle - width / 2.0
                if self.spec2 > 0 and isinstance(self, spectrum_classes.CurrentContour):
                    self.ax.set_ylim(self.ymaxlim, self.yminlim)
                else:
                    self.ax.set_ylim(self.yminlim, self.ymaxlim)
            else:
                middle = (np.log(self.ymaxlim) + np.log(self.yminlim)) / 2.0
                width = np.log(self.ymaxlim) - np.log(self.yminlim)
                width = width * 0.9**event.step
                self.ymaxlim = np.exp(middle + width / 2.0)
                self.yminlim = np.exp(middle - width / 2.0)
                if self.spec2 > 0 and isinstance(self, spectrum_classes.CurrentContour):
                    self.ax.set_ylim(self.ymaxlim, self.yminlim)
                else:
                    self.ax.set_ylim(self.yminlim, self.ymaxlim)
        self.canvas.draw()

    def buttonRelease(self, event):
        if event.button == 1:
            if self.peakPick:
                if self.rect[0] is not None:
                    self.rect[0].remove()
                    self.rect[0] = None
                    self.peakPick = False
                    idx = np.argmin(np.abs(self.line_xdata - event.xdata))
                    if self.peakPickFunc is not None:
                        self.peakPickFunc((idx, self.line_xdata[idx], self.line_ydata[idx]))
                    if not self.peakPick:  # check if peakpicking is still required
                        self.peakPickFunc = None
            else:
                self.leftMouse = False
                if self.rect[0] is not None:
                    self.rect[0].remove()
                if self.rect[1] is not None:
                    self.rect[1].remove()
                if self.rect[2] is not None:
                    self.rect[2].remove()
                if self.rect[3] is not None:
                    self.rect[3].remove()
                self.rect = [None, None, None, None]
                if self.zoomX2 is not None and self.zoomY2 is not None:
                    self.xminlim = min([self.zoomX1, self.zoomX2])
                    self.xmaxlim = max([self.zoomX1, self.zoomX2])
                    self.yminlim = min([self.zoomY1, self.zoomY2])
                    self.ymaxlim = max([self.zoomY1, self.zoomY2])
                    if self.spec > 0 and not isinstance(self, spectrum_classes.CurrentArrayed):
                        self.ax.set_xlim(self.xmaxlim, self.xminlim)
                    else:
                        self.ax.set_xlim(self.xminlim, self.xmaxlim)
                    if self.spec2 > 0 and isinstance(self, spectrum_classes.CurrentContour):
                        self.ax.set_ylim(self.ymaxlim, self.yminlim)
                    else:
                        self.ax.set_ylim(self.yminlim, self.ymaxlim)
                self.zoomX1 = None
                self.zoomX2 = None
                self.zoomY1 = None
                self.zoomY2 = None
        elif event.button == 3:
            self.rightMouse = False
        self.canvas.draw()

    def pan(self, event):
        if self.rightMouse and self.panX is not None and self.panY is not None:
            if self.logx == 0 and self.logy == 0:
                inv = self.ax.transData.inverted()
                point = inv.transform((event.x, event.y))
                x = point[0]
                y = point[1]
            else:
                x = event.xdata
                y = event.ydata
                if x is None or y is None:
                    return
            if self.logx == 0:
                diffx = x - self.panX
                self.xmaxlim = self.xmaxlim - diffx
                self.xminlim = self.xminlim - diffx
            else:
                diffx = np.log(x) - np.log(self.panX)
                self.xmaxlim = np.exp(np.log(self.xmaxlim) - diffx)
                self.xminlim = np.exp(np.log(self.xminlim) - diffx)
            if self.logy == 0:
                diffy = y - self.panY
                self.ymaxlim = self.ymaxlim - diffy
                self.yminlim = self.yminlim - diffy
            else:
                diffy = np.log(y) - np.log(self.panY)
                self.ymaxlim = np.exp(np.log(self.ymaxlim) - diffy)
                self.yminlim = np.exp(np.log(self.yminlim) - diffy)
            if self.spec > 0 and not isinstance(self, spectrum_classes.CurrentArrayed):
                self.ax.set_xlim(self.xmaxlim, self.xminlim)
            else:
                self.ax.set_xlim(self.xminlim, self.xmaxlim)
            if self.spec2 > 0 and isinstance(self, spectrum_classes.CurrentContour):
                self.ax.set_ylim(self.ymaxlim, self.yminlim)
            else:
                self.ax.set_ylim(self.yminlim, self.ymaxlim)
            self.canvas.draw()
        elif self.peakPick:
            if self.rect[0] is not None:
                self.rect[0].remove()
                self.rect[0] = None
            if event.xdata is not None:
                self.rect[0] = self.ax.axvline(event.xdata, c='k', linestyle='--')
            self.canvas.draw()
        elif self.leftMouse and (self.zoomX1 is not None) and (self.zoomY1 is not None):
            if self.logx == 0 and self.logy == 0:
                inv = self.ax.transData.inverted()
                point = inv.transform((event.x, event.y))
                self.zoomX2 = point[0]
                self.zoomY2 = point[1]
            else:
                self.zoomX2 = event.xdata
                self.zoomY2 = event.ydata
                if self.zoomX2 is None or self.zoomY2 is None:
                    return
            if self.rect[0] is not None:
                if self.rect[0] is not None:
                    self.rect[0].remove()
                if self.rect[1] is not None:
                    self.rect[1].remove()
                if self.rect[2] is not None:
                    self.rect[2].remove()
                if self.rect[3] is not None:
                    self.rect[3].remove()
                self.rect = [None, None, None, None]
            self.rect[0], = self.ax.plot([self.zoomX1, self.zoomX2], [self.zoomY2, self.zoomY2], 'k', clip_on=False)
            self.rect[1], = self.ax.plot([self.zoomX1, self.zoomX2], [self.zoomY1, self.zoomY1], 'k', clip_on=False)
            self.rect[2], = self.ax.plot([self.zoomX1, self.zoomX1], [self.zoomY1, self.zoomY2], 'k', clip_on=False)
            self.rect[3], = self.ax.plot([self.zoomX2, self.zoomX2], [self.zoomY1, self.zoomY2], 'k', clip_on=False)
            self.canvas.draw()

    def setLog(self, logx, logy):
        self.logx = logx
        self.logy = logy
        self.ax.set_xlim(self.xminlim, self.xmaxlim)
        self.ax.set_ylim(self.yminlim, self.ymaxlim)

#################################################################################


class DiffusionParamFrame(AbstractParamFrame):

    def __init__(self, parent, rootwindow, isMain=True):
        self.SINGLENAMES = ['amp', 'const']
        self.MULTINAMES = ['coeff', 'd']
        self.FITFUNC = diffusionmpFit
        super(DiffusionParamFrame, self).__init__(parent, rootwindow, isMain)
        locList = tuple(self.parent.locList)
        self.ticks = {'amp':[], 'const':[], 'coeff':[], 'd':[]}
        self.entries = {'amp':[], 'const':[], 'coeff':[], 'd':[]}
        self.frame2.addWidget(QLabel(u"\u03b3 [MHz/T]:"), 0, 0)
        self.gammaEntry = QtWidgets.QLineEdit()
        self.gammaEntry.setAlignment(QtCore.Qt.AlignHCenter)
        self.gammaEntry.setText("42.576")
        self.frame2.addWidget(self.gammaEntry, 1, 0)
        self.frame2.addWidget(QLabel(u"\u03b4 [s]:"), 2, 0)
        self.deltaEntry = QtWidgets.QLineEdit()
        self.deltaEntry.setAlignment(QtCore.Qt.AlignHCenter)
        self.deltaEntry.setText("1.0")
        self.frame2.addWidget(self.deltaEntry, 3, 0)
        self.frame2.addWidget(QLabel(u"\u0394 [s]:"), 4, 0)
        self.triangleEntry = QtWidgets.QLineEdit()
        self.triangleEntry.setAlignment(QtCore.Qt.AlignHCenter)
        self.triangleEntry.setText("1.0")
        self.frame2.addWidget(self.triangleEntry, 5, 0)
        self.frame2.setColumnStretch(10, 1)
        self.frame2.setAlignment(QtCore.Qt.AlignTop)
        self.frame3.addWidget(QLabel("Amplitude:"), 0, 0, 1, 2)
        self.ticks['amp'].append(QtWidgets.QCheckBox(''))
        self.frame3.addWidget(self.ticks['amp'][-1], 1, 0)
        self.entries['amp'].append(QtWidgets.QLineEdit())
        self.entries['amp'][-1].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['amp'][-1].setText(('%#.'+str(self.rootwindow.tabWindow.PRECIS)+'g') % np.amax(self.parent.data1D))
        self.frame3.addWidget(self.entries['amp'][-1], 1, 1)
        self.frame3.addWidget(QLabel("Constant:"), 2, 0, 1, 2)
        self.ticks['const'].append(QtWidgets.QCheckBox(''))
        self.ticks['const'][-1].setChecked(True)
        self.frame3.addWidget(self.ticks['const'][-1], 3, 0)
        self.entries['const'].append(QtWidgets.QLineEdit())
        self.entries['const'][-1].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['const'][-1].setText("0.0")
        self.frame3.addWidget(self.entries['const'][-1], 3, 1)
        self.frame3.setColumnStretch(10, 1)
        self.frame3.setAlignment(QtCore.Qt.AlignTop)
        self.numExp = QtWidgets.QComboBox()
        self.numExp.addItems(['1', '2', '3', '4'])
        self.numExp.currentIndexChanged.connect(self.changeNum)
        self.frame4.addWidget(self.numExp, 0, 0, 1, 2)
        self.frame4.addWidget(QLabel("Coefficient:"), 1, 0, 1, 2)
        self.frame4.addWidget(QLabel("D [m^2/s]:"), 1, 2, 1, 2)
        self.frame4.setColumnStretch(20, 1)
        self.frame4.setAlignment(QtCore.Qt.AlignTop)
        self.xlog = QtWidgets.QCheckBox('x-log')
        self.xlog.stateChanged.connect(self.setLog)
        self.optframe.addWidget(self.xlog, 0, 0)
        self.ylog = QtWidgets.QCheckBox('y-log')
        self.ylog.stateChanged.connect(self.setLog)
        self.optframe.addWidget(self.ylog, 1, 0)
        self.optframe.setColumnStretch(10, 1)
        self.optframe.setAlignment(QtCore.Qt.AlignTop)
        self.coeffEntries = []
        self.coeffTicks = []
        self.dEntries = []
        self.dTicks = []
        for i in range(self.FITNUM):
            for j in range(len(self.MULTINAMES)):
                self.ticks[self.MULTINAMES[j]].append(QtWidgets.QCheckBox(''))
                self.ticks[self.MULTINAMES[j]][i].setChecked(self.fitParamList[locList][self.MULTINAMES[j]][i][1])
                self.frame4.addWidget(self.ticks[self.MULTINAMES[j]][i], i + 2, 2*j)
                self.entries[self.MULTINAMES[j]].append(QtWidgets.QLineEdit())
                self.entries[self.MULTINAMES[j]][i].setAlignment(QtCore.Qt.AlignHCenter)
                self.entries[self.MULTINAMES[j]][i].setText(('%#.'+str(self.rootwindow.tabWindow.PRECIS)+'g') % self.fitParamList[locList][self.MULTINAMES[j]][i][0])
                self.frame4.addWidget(self.entries[self.MULTINAMES[j]][i], i + 2, 2*j+1)
                if i > 0:
                    self.ticks[self.MULTINAMES[j]][i].hide()
                    self.entries[self.MULTINAMES[j]][i].hide()

    def defaultValues(self, inp):
        if not inp:
            return {'amp':[np.amax(self.parent.data1D), False], 'const':[0.0, False], 'coeff':np.repeat([np.array([1.0, False], dtype=object)], self.FITNUM, axis=0), 'd':np.repeat([np.array([1.0e-9, False], dtype=object)], self.FITNUM,axis=0)}
        else:
            return inp

    def setLog(self, *args):
        self.parent.setLog(self.xlog.isChecked(), self.ylog.isChecked())
        self.sim()

    def getExtraParams(self, out):
        out['gamma'] = [safeEval(self.gammaEntry.text())]
        out['delta'] = [safeEval(self.deltaEntry.text())]
        out['triangle'] = [safeEval(self.triangleEntry.text())]
        return (out, [out['gamma'][-1], out['delta'][-1], out['triangle'][-1]])
        
    def disp(self, params, num, display=True, prepExport=False):
        out = params[num]
        for name in ['amp', 'const', 'gamma', 'delta', 'triangle']:
            inp = out[name][0]
            if isinstance(inp, tuple):
                inp = checkLinkTuple(inp)
                out[name][0] = inp[2]*params[inp[4]][inp[0]][inp[1]] + inp[3]
        numExp = len(out['coeff'])
        for i in range(numExp):
            for name in ['coeff', 'd']:
                inp = out[name][i]
                if isinstance(inp, tuple):
                    inp = checkLinkTuple(inp)
                    out[name][i] = inp[2]*params[inp[4]][inp[0]][inp[1]] + inp[3]
                if not np.isfinite(out[name][i]):
                    self.rootwindow.mainProgram.dispMsg("One of the inputs is not valid")
                    return
        tmpx = self.parent.xax
        if prepExport:
            x = tmpx
            outCurve = out['const'][0]*np.ones(len(x))
        else:
            numCurve = 100  # number of points in output curve
            outCurve = out['const'][0]*np.ones(numCurve)
            if self.xlog.isChecked():
                x = np.logspace(np.log(min(tmpx)), np.log(max(tmpx)), numCurve)
            else:
                x = np.linspace(min(tmpx), max(tmpx), numCurve)
        for i in range(len(out['coeff'])):
            outCurve += out['coeff'][i] * np.exp(-(out['gamma'][0] * out['delta'][0] * x)**2 * out['d'][i] * (out['triangle'][0] - out['delta'][0] / 3.0))
        self.parent.fitDataList[tuple(self.parent.locList)] = [x, out['amp'][0]*outCurve, [], []]
        if display:
            self.parent.showFid()

    def calculateResultsToWorkspace(self, prepExport):
        self.rootwindow.sim(display=False, prepExport=prepExport)

##############################################################################


def diffusionmpFit(xax, data1D, guess, args, queue, minmethod):
    try:
        fitVal = scipy.optimize.minimize(lambda *param: np.sum((data1D-diffusionfitFunc(param, xax, args))**2), guess, method=minmethod)
    except:
        fitVal = None
    queue.put(fitVal)

def diffusionfitFunc(params, allX, args):
    params = params[0]
    specName = args[0]
    specSlices = args[1]
    allParam = []
    for length in specSlices:
        allParam.append(params[length])
    allStruc = args[3]
    allArgu = args[4]
    fullTestFunc = []
    for n in range(len(allX)):
        x=allX[n]
        testFunc = np.zeros(len(x))
        param = allParam[n]
        numExp = args[2][n]
        struc = args[3][n]
        argu = args[4][n]
        sw = args[5][n]
        axAdd = args[6][n]
        axMult = args[7][n]
        parameters = {'amp':0.0, 'const':0.0, 'coeff':0.0, 'd':0.0}
        parameters['gamma'] = argu[-1][0]
        parameters['delta'] = argu[-1][1]
        parameters['triangle'] = argu[-1][2]
        for name in ['amp', 'const']:
            if struc[name][0][0] == 1:
                parameters[name] = param[struc[name][0][1]]
            elif struc[name][0][0] == 0:
                parameters[name] = argu[struc[name][0][1]]
            else:
                altStruc = struc[name][0][1]
                if struc[altStruc[0]][altStruc[1]][0] == 1:
                    parameters[name] = altStruc[2] * allParam[altStruc[4]][struc[altStruc[0]][altStruc[1]][1]] + altStruc[3]
                elif struc[altStruc[0]][altStruc[1]][0] == 0:
                    parameters[name] = altStruc[2] * allArgu[altStruc[4]][struc[altStruc[0]][altStruc[1]][1]] + altStruc[3]
        for i in range(numExp):
            for name in ['coeff', 'd']:
                if struc[name][i][0] == 1:
                    parameters[name] = param[struc[name][i][1]]
                elif struc[name][i][0] == 0:
                    parameters[name] = argu[struc[name][i][1]]
                else:
                    altStruc = struc[name][i][1]
                    strucTarget = allStruc[altStruc[4]]
                    if strucTarget[altStruc[0]][altStruc[1]][0] == 1:
                        parameters[name] = altStruc[2] * allParam[altStruc[4]][strucTarget[altStruc[0]][altStruc[1]][1]] + altStruc[3]
                    elif strucTarget[altStruc[0]][altStruc[1]][0] == 0:
                        parameters[name] = altStruc[2] * allArgu[altStruc[4]][strucTarget[altStruc[0]][altStruc[1]][1]] + altStruc[3]
            testFunc += parameters['coeff'] * np.exp(-(parameters['gamma'] * parameters['delta'] * x)**2 * parameters['d'] * (parameters['triangle'] - parameters['delta'] / 3.0))
        fullTestFunc = np.append(fullTestFunc, parameters['amp'] * (parameters['const'] + testFunc))
    return fullTestFunc

##############################################################################


class PeakDeconvWindow(TabFittingWindow):

    def __init__(self, mainProgram, oldMainWindow):
        self.CURRENTWINDOW = PeakDeconvFrame
        self.PARAMFRAME = PeakDeconvParamFrame
        super(PeakDeconvWindow, self).__init__(mainProgram, oldMainWindow)

#################################################################################


class PeakDeconvFrame(FitPlotFrame):

    def __init__(self, rootwindow, fig, canvas, current):
        self.FITNUM = 10 # Maximum number of fits
        super(PeakDeconvFrame, self).__init__(rootwindow, fig, canvas, current)

    def togglePick(self, var):
        self.peakPickReset()
        if var == 1:
            self.peakPickFunc = lambda pos, self=self: self.pickDeconv(pos)
            self.peakPick = True
        else:
            self.peakPickFunc = None
            self.peakPick = False

    def pickDeconv(self, pos):
        pickNum = self.fitPickNumList[tuple(self.locList)]
        if self.pickWidth:
            if self.current.spec == 1:
                if self.current.ppm:
                    axMult = 1e6 / self.current.ref
                else:
                    axMult = 1.0 / (1000.0**self.current.axType)
            elif self.current.spec == 0:
                axMult = 1000.0**self.current.axType
            width = (2 * abs(float(self.rootwindow.paramframe.entries['pos'][pickNum].text()) - pos[1])) / axMult
            self.rootwindow.paramframe.entries['amp'][pickNum].setText(('%#.'+str(self.rootwindow.tabWindow.PRECIS)+'g') % (float(self.rootwindow.paramframe.entries['amp'][pickNum].text()) * width))
            self.rootwindow.paramframe.entries['lor'][pickNum].setText(('%#.'+str(self.rootwindow.tabWindow.PRECIS)+'g') % abs(width))
            self.fitPickNumList[tuple(self.locList)] += 1
            self.pickWidth = False
            self.rootwindow.sim()
        else:
            self.rootwindow.paramframe.entries['pos'][pickNum].setText(('%#.'+str(self.rootwindow.tabWindow.PRECIS)+'g') % pos[1])
            left = pos[0] - self.FITNUM
            if left < 0:
                left = 0
            right = pos[0] + self.FITNUM
            if right >= len(self.data1D):
                right = len(self.data1D) - 1
            self.rootwindow.paramframe.entries['amp'][pickNum].setText(('%#.'+str(self.rootwindow.tabWindow.PRECIS)+'g') % (pos[2] * np.pi * 0.5))
            if pickNum < self.FITNUM:
                self.rootwindow.paramframe.numExp.setCurrentIndex(pickNum)
                self.rootwindow.paramframe.changeNum()
            self.pickWidth = True
        if pickNum < self.FITNUM:
            self.peakPickFunc = lambda pos, self=self: self.pickDeconv(pos)
            self.peakPick = True

#################################################################################


class PeakDeconvParamFrame(AbstractParamFrame):

    def __init__(self, parent, rootwindow, isMain=True):
        self.SINGLENAMES = ['bgrnd', 'slope']
        self.MULTINAMES = ['pos', 'amp', 'lor', 'gauss']
        self.FITFUNC = peakDeconvmpFit
        #Get full integral
        self.fullInt = np.sum(parent.data1D) * parent.current.sw / float(len(parent.data1D))
        super(PeakDeconvParamFrame, self).__init__(parent, rootwindow, isMain)
        resetButton = QtWidgets.QPushButton("Reset")
        resetButton.clicked.connect(self.reset)
        self.frame1.addWidget(resetButton, 1, 1)
        self.pickTick = QtWidgets.QCheckBox("Pick")
        self.pickTick.stateChanged.connect(self.togglePick)
        self.frame1.addWidget(self.pickTick, 2, 1)
        self.ticks = {'bgrnd':[], 'slope':[], 'pos':[], 'amp':[], 'lor':[], 'gauss':[]}
        self.entries = {'bgrnd':[], 'slope':[], 'pos':[], 'amp':[], 'lor':[], 'gauss':[], 'method':[]}
        self.frame2.addWidget(QLabel("Bgrnd:"), 0, 0, 1, 2)
        self.ticks['bgrnd'].append(QtWidgets.QCheckBox(''))
        self.frame2.addWidget(self.ticks['bgrnd'][0], 1, 0)
        self.entries['bgrnd'].append(QtWidgets.QLineEdit())
        self.entries['bgrnd'][0].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['bgrnd'][0].setText("0.0")
        self.frame2.addWidget(self.entries['bgrnd'][0], 1, 1)
        self.frame2.addWidget(QLabel("Slope:"), 2, 0, 1, 2)
        self.ticks['slope'].append(QtWidgets.QCheckBox(''))
        self.frame2.addWidget(self.ticks['slope'][0], 3, 0)
        self.entries['slope'].append(QtWidgets.QLineEdit())
        self.entries['slope'][0].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['slope'][0].setText("0.0")
        self.frame2.addWidget(self.entries['slope'][0], 3, 1)
        self.frame2.addWidget(QLabel("Method:"), 4, 0, 1, 2)
        self.entries['method'].append(QtWidgets.QComboBox())
        self.entries['method'][0].addItems(['Exact','Approx'])
        self.frame2.addWidget(self.entries['method'][0], 5, 1)
        self.frame2.setColumnStretch(self.FITNUM, 1)
        self.frame2.setAlignment(QtCore.Qt.AlignTop)
        self.numExp = QtWidgets.QComboBox()
        self.numExp.addItems([str(x+1) for x in range(self.FITNUM)])
        self.numExp.currentIndexChanged.connect(self.changeNum)
        self.frame3.addWidget(self.numExp, 0, 0, 1, 2)
        self.frame3.addWidget(QLabel("Position [" + self.axUnit + "]:"), 1, 0, 1, 2)
        self.frame3.addWidget(QLabel("Integral:"), 1, 2, 1, 2)
        self.frame3.addWidget(QLabel("Lorentz [Hz]:"), 1, 4, 1, 2)
        self.frame3.addWidget(QLabel("Gauss [Hz]:"), 1, 6, 1, 2)
        self.frame3.setColumnStretch(20, 1)
        self.frame3.setAlignment(QtCore.Qt.AlignTop)
        for i in range(self.FITNUM):
            for j in range(len(self.MULTINAMES)):
                self.ticks[self.MULTINAMES[j]].append(QtWidgets.QCheckBox(''))
                self.frame3.addWidget(self.ticks[self.MULTINAMES[j]][i], i + 2, 2*j)
                self.entries[self.MULTINAMES[j]].append(QtWidgets.QLineEdit())
                self.entries[self.MULTINAMES[j]][i].setAlignment(QtCore.Qt.AlignHCenter)
                self.frame3.addWidget(self.entries[self.MULTINAMES[j]][i], i + 2, 2*j+1)
        self.reset()

    def defaultValues(self, inp):
        if not inp:
            return {'bgrnd':[0.0, True], 'slope':[0.0, True], 'pos':np.repeat([np.array([0.0, False], dtype=object)], self.FITNUM, axis=0), 'amp':np.repeat([np.array([self.fullInt, False], dtype=object)], self.FITNUM,axis=0), 'lor':np.repeat([np.array([1.0, False], dtype=object)], self.FITNUM,axis=0), 'gauss':np.repeat([np.array([0.0, True], dtype=object)], self.FITNUM,axis=0)}
        else:
            return inp
        
    def reset(self):
        locList = tuple(self.parent.locList)
        self.fitNumList[locList] = 0
        for name in ['bgrnd', 'slope']:
            self.fitParamList[locList][name] = [0.0, True]
        self.pickTick.setChecked(True)
        defaults = {'pos':[0.0,False], 'amp':[self.fullInt,False], 'lor':[1.0,False], 'gauss':[0.0,True]}
        for i in range(self.FITNUM):
            for name in defaults.keys():
                self.fitParamList[locList][name][i] = defaults[name]
        self.togglePick()
        self.parent.pickWidth = False
        self.parent.fitPickNumList[locList] = 0
        self.dispParams()

    def togglePick(self):
        self.parent.togglePick(self.pickTick.isChecked())

    def getExtraParams(self, out):
        out['method'] = [self.entries['method'][0].currentIndex()]
        return (out, [out['method'][-1]])
        
    def disp(self, params, num):
        out = params[num]
        for name in self.SINGLENAMES:
            inp = out[name][0]
            if isinstance(inp, tuple):
                inp = checkLinkTuple(inp)
                out[name][0] = inp[2]*params[inp[4]][inp[0]][inp[1]] + inp[3]
        numExp = len(out['pos'])
        for i in range(numExp):
            for name in self.MULTINAMES:
                inp = out[name][i]
                if isinstance(inp, tuple):
                    inp = checkLinkTuple(inp)
                    out[name][i] = inp[2]*params[inp[4]][inp[0]][inp[1]] + inp[3]
                if not np.isfinite(out[name][i]):
                    self.rootwindow.mainProgram.dispMsg("One of the inputs is not valid")
                    return
        tmpx = self.parent.xax
        outCurveBase = out['bgrnd'][0] + tmpx * out['slope'][0]
        outCurve = outCurveBase.copy()
        outCurvePart = []
        x = []
        for i in range(len(out['amp'])):
            x.append(tmpx)
            pos = out['pos'][i] / self.axMult
            y = voigtLine(tmpx, pos, out['lor'][i], out['gauss'][i], out['amp'][i], out['method'][0])
            outCurvePart.append(outCurveBase + y)
            outCurve += y
        self.parent.fitDataList[tuple(self.parent.locList)] = [tmpx, outCurve, x, outCurvePart]
        self.parent.showFid()

##############################################################################


def peakDeconvmpFit(xax, data1D, guess, args, queue, minmethod):
    try:
        fitVal = scipy.optimize.minimize(lambda *param: np.sum((data1D-peakDeconvfitFunc(param, xax, args))**2), guess, method=minmethod)
    except:
        fitVal = None
    queue.put(fitVal)

def peakDeconvfitFunc(params, allX, args):
    params = params[0]
    specName = args[0]
    specSlices = args[1]
    allParam = []
    for length in specSlices:
        allParam.append(params[length])
    allStruc = args[3]
    allArgu = args[4]
    fullTestFunc = []
    for n in range(len(allX)):
        x=allX[n]
        testFunc = np.zeros(len(x))
        param = allParam[n]
        numExp = args[2][n]
        struc = args[3][n]
        argu = args[4][n]
        sw = args[5][n]
        axAdd = args[6][n]
        axMult = args[7][n]
        parameters = {'bgrnd':0.0, 'slope':0.0, 'pos':0.0, 'amp':0.0, 'lor':0.0, 'gauss':0.0}
        parameters['method'] = argu[-1][0]
        for name in ['bgrnd', 'slope']:
            if struc[name][0][0] == 1:
                parameters[name] = param[struc[name][0][1]]
            elif struc[name][0][0] == 0:
                parameters[name] = argu[struc[name][0][1]]
            else:
                altStruc = struc[name][0][1]
                if struc[altStruc[0]][altStruc[1]][0] == 1:
                    parameters[name] = altStruc[2] * allParam[altStruc[4]][struc[altStruc[0]][altStruc[1]][1]] + altStruc[3]
                elif struc[altStruc[0]][altStruc[1]][0] == 0:
                    parameters[name] = altStruc[2] * allArgu[altStruc[4]][struc[altStruc[0]][altStruc[1]][1]] + altStruc[3]
        for i in range(numExp):
            for name in ['pos', 'amp', 'lor', 'gauss']:
                if struc[name][i][0] == 1:
                    parameters[name] = param[struc[name][i][1]]
                elif struc[name][i][0] == 0:
                    parameters[name] = argu[struc[name][i][1]]
                else:
                    altStruc = struc[name][i][1]
                    strucTarget = allStruc[altStruc[4]]
                    if strucTarget[altStruc[0]][altStruc[1]][0] == 1:
                        parameters[name] = altStruc[2] * allParam[altStruc[4]][strucTarget[altStruc[0]][altStruc[1]][1]] + altStruc[3]
                    elif strucTarget[altStruc[0]][altStruc[1]][0] == 0:
                        parameters[name] = altStruc[2] * allArgu[altStruc[4]][strucTarget[altStruc[0]][altStruc[1]][1]] + altStruc[3]
            pos = parameters['pos'] / axMult
            testFunc += voigtLine(x, pos, parameters['lor'], parameters['gauss'], parameters['amp'], parameters['method'])    
        testFunc += parameters['bgrnd'] + parameters['slope'] * x
        fullTestFunc = np.append(fullTestFunc, testFunc)
    return fullTestFunc

##############################################################################


class TensorDeconvWindow(TabFittingWindow):

    def __init__(self, mainProgram, oldMainWindow):
        self.CURRENTWINDOW = TensorDeconvFrame
        self.PARAMFRAME = TensorDeconvParamFrame
        super(TensorDeconvWindow, self).__init__(mainProgram, oldMainWindow)

#####################################################################################


class TensorDeconvFrame(FitPlotFrame):

    def __init__(self, rootwindow, fig, canvas, current):
        self.FITNUM = 10 # Maximum number of fits
        self.pickNum = 0
        self.pickNum2 = 0
        super(TensorDeconvFrame, self).__init__(rootwindow, fig, canvas, current)

    def togglePick(self, var):
        self.peakPickReset()
        if var == 1:
            self.peakPickFunc = lambda pos, self=self: self.pickDeconv(pos)
            self.peakPick = True
        else:
            self.peakPickFunc = None
            self.peakPick = False

    def pickDeconv(self, pos):
        printStr = "%#." + str(self.rootwindow.tabWindow.PRECIS) + "g"
        if self.spec == 1:
            if self.current.ppm:
                axMult = 1e6 / self.current.ref
            else:
                axMult = 1.0 / (1000.0**self.current.axType)
        elif self.spec == 0:
            axMult = 1000.0**self.current.axType
        if self.pickNum2 == 0:
            if self.pickNum < self.FITNUM:
                self.rootwindow.paramframe.numExp.setCurrentIndex(self.pickNum)
                self.rootwindow.paramframe.changeNum()
            self.rootwindow.paramframe.entries['t11'][self.pickNum].setText(printStr % (self.xax[pos[0]]*axMult))
            self.pickNum2 = 1
        elif self.pickNum2 == 1:
            self.rootwindow.paramframe.entries['t22'][self.pickNum].setText(printStr % (self.xax[pos[0]]*axMult))
            self.pickNum2 = 2
        elif self.pickNum2 == 2:
            self.rootwindow.paramframe.entries['t33'][self.pickNum].setText(printStr % (self.xax[pos[0]]*axMult))
            self.pickNum2 = 0
            self.pickNum += 1
        if self.pickNum < self.FITNUM:
            self.peakPickFunc = lambda pos, self=self: self.pickDeconv(pos)
            self.peakPick = True

#################################################################################


class TensorDeconvParamFrame(AbstractParamFrame):

    def __init__(self, parent, rootwindow, isMain=True):
        self.SINGLENAMES = ['bgrnd', 'slope', 'spinspeed']
        self.MULTINAMES = ['t11', 't22', 't33', 'amp', 'lor', 'gauss']
        self.FITFUNC = tensorDeconvmpFit

        #Get full integral
        self.fullInt = np.sum(parent.data1D) * parent.current.sw / float(len(parent.data1D))

        self.cheng = 15
        super(TensorDeconvParamFrame, self).__init__(parent, rootwindow, isMain)
        resetButton = QtWidgets.QPushButton("Reset")
        resetButton.clicked.connect(self.reset)
        self.frame1.addWidget(resetButton, 1, 1)
        self.pickTick = QtWidgets.QCheckBox("Pick")
        self.pickTick.stateChanged.connect(self.togglePick)
        self.frame1.addWidget(self.pickTick, 2, 1)
        self.ticks = {'bgrnd':[], 'slope':[], 'spinspeed':[], 't11':[], 't22':[], 't33':[], 'amp':[], 'lor':[], 'gauss':[]}
        self.entries = {'bgrnd':[], 'slope':[], 'spinspeed':[], 't11':[], 't22':[], 't33':[], 'amp':[], 'lor':[], 'gauss':[], 'shiftdef':[], 'cheng':[], 'mas':[], 'numssb':[]}

        self.optframe.addWidget(QLabel("Cheng:"), 0, 0)
        self.entries['cheng'].append(QtWidgets.QSpinBox())
        self.entries['cheng'][-1].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['cheng'][-1].setValue(self.cheng)
        self.optframe.addWidget(self.entries['cheng'][-1], 1, 0)
        self.entries['mas'].append(QtWidgets.QCheckBox('Spinning'))
        self.optframe.addWidget(self.entries['mas'][-1], 2, 0)
        self.optframe.addWidget(QLabel("# sidebands:"), 3, 0)
        self.entries['numssb'].append(QtWidgets.QSpinBox())
        self.entries['numssb'][-1].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['numssb'][-1].setValue(32)
        self.optframe.addWidget(self.entries['numssb'][-1], 4, 0)
        self.shiftDefType = 0 #variable to remember the selected tensor type
        self.optframe.addWidget(QLabel("Definition:"), 5, 0)
        self.entries['shiftdef'].append(QtWidgets.QComboBox())
        self.entries['shiftdef'][-1].addItems([u'\u03b411 - \u03b422 - \u03b433',
                                               u'\u03b4xx - \u03b4yy - \u03b4zz',
                                               u'\u03b4iso - \u03b4aniso - \u03b7',
                                               u'\u03b4iso - \u03a9 - \u03b7'])
        self.entries['shiftdef'][-1].currentIndexChanged.connect(self.changeShiftDef)
        self.optframe.addWidget(self.entries['shiftdef'][-1], 6, 0)
        self.optframe.setColumnStretch(10, 1)
        self.optframe.setAlignment(QtCore.Qt.AlignTop)
        self.spinLabel = QLabel("Spin. speed [kHz]:")
        self.frame2.addWidget(self.spinLabel, 0, 0, 1, 2)
        self.ticks['spinspeed'].append(QtWidgets.QCheckBox(''))
        self.frame2.addWidget(self.ticks['spinspeed'][-1], 1, 0)
        self.entries['spinspeed'].append(QtWidgets.QLineEdit())
        self.entries['spinspeed'][-1].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['spinspeed'][-1].setText("10.0")
        self.frame2.addWidget(self.entries['spinspeed'][-1], 1, 1)
        self.frame2.addWidget(QLabel("Bgrnd:"), 2, 0, 1, 2)
        self.ticks['bgrnd'].append(QtWidgets.QCheckBox(''))
        self.frame2.addWidget(self.ticks['bgrnd'][-1], 3, 0)
        self.entries['bgrnd'].append(QtWidgets.QLineEdit())
        self.entries['bgrnd'][-1].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['bgrnd'][-1].setText("0.0")
        self.frame2.addWidget(self.entries['bgrnd'][-1], 3, 1)
        self.frame2.addWidget(QLabel("Slope:"), 4, 0, 1, 2)
        self.ticks['slope'].append(QtWidgets.QCheckBox(''))
        self.frame2.addWidget(self.ticks['slope'][-1], 5, 0)
        self.entries['slope'].append(QtWidgets.QLineEdit())
        self.entries['slope'][-1].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['slope'][-1].setText("0.0")
        self.frame2.addWidget(self.entries['slope'][-1], 5, 1)
        self.frame2.setColumnStretch(10, 1)
        self.frame2.setAlignment(QtCore.Qt.AlignTop)
        self.numExp = QtWidgets.QComboBox()
        self.numExp.addItems([str(x+1) for x in range(self.FITNUM)])
        self.numExp.currentIndexChanged.connect(self.changeNum)
        self.frame3.addWidget(self.numExp, 0, 0, 1, 2)
        if self.parent.current.ppm:
            axUnit = 'ppm'
        else:
            axUnit = ['Hz', 'kHz', 'MHz'][self.parent.current.axType]
        #Labels
        self.label11 = QLabel(u'\u03b4' + '<sub>11</sub> [' + axUnit + '] :')
        self.label22 = QLabel(u'\u03b4' + '<sub>22</sub> [' + axUnit + '] :')
        self.label33 = QLabel(u'\u03b4' + '<sub>33</sub> [' + axUnit + '] :')
        self.frame3.addWidget(self.label11, 1, 0, 1, 2)
        self.frame3.addWidget(self.label22, 1, 2, 1, 2)
        self.frame3.addWidget(self.label33, 1, 4, 1, 2)
        self.labelxx = QLabel(u'\u03b4' + '<sub>xx</sub> [' + axUnit + '] :')
        self.labelyy = QLabel(u'\u03b4' + '<sub>yy</sub> [' + axUnit + '] :')
        self.labelzz = QLabel(u'\u03b4' + '<sub>zz</sub> [' + axUnit + '] :')
        self.labelxx.hide()
        self.labelyy.hide()
        self.labelzz.hide()
        self.frame3.addWidget(self.labelxx, 1, 0, 1, 2)
        self.frame3.addWidget(self.labelyy, 1, 2, 1, 2)
        self.frame3.addWidget(self.labelzz, 1, 4, 1, 2)
        self.labeliso = QLabel(u'\u03b4' + '<sub>iso</sub> [' + axUnit + '] :')
        self.labelaniso = QLabel(u'\u03b4' + '<sub>aniso</sub> [' + axUnit  +'] :')
        self.labeleta = QLabel(u'\u03b7:')
        self.labeliso.hide()
        self.labelaniso.hide()
        self.labeleta.hide()
        self.frame3.addWidget(self.labeliso, 1, 0, 1, 2)
        self.frame3.addWidget(self.labelaniso, 1, 2, 1, 2)
        self.frame3.addWidget(self.labeleta, 1, 4, 1, 2)
        self.labeliso2 = QLabel(u'\u03b4' + '<sub>iso</sub> [' + axUnit  +'] :')
        self.labelspan = QLabel(u'\u03a9 [' + axUnit  +'] :')
        self.labelskew = QLabel(u'\u03ba:')
        self.labeliso2.hide()
        self.labelspan.hide()
        self.labelskew.hide()
        self.frame3.addWidget(self.labeliso2, 1, 0, 1, 2)
        self.frame3.addWidget(self.labelspan, 1, 2, 1, 2)
        self.frame3.addWidget(self.labelskew, 1, 4, 1, 2)
        self.frame3.addWidget(QLabel("Integral:"), 1, 6, 1, 2)
        self.frame3.addWidget(QLabel("Lorentz [Hz]:"), 1, 8, 1, 2)
        self.frame3.addWidget(QLabel("Gauss [Hz]:"), 1, 10, 1, 2)
        self.frame3.setColumnStretch(20, 1)
        self.frame3.setAlignment(QtCore.Qt.AlignTop)
        for i in range(self.FITNUM):
            for j in range(len(self.MULTINAMES)):
                self.ticks[self.MULTINAMES[j]].append(QtWidgets.QCheckBox(''))
                self.frame3.addWidget(self.ticks[self.MULTINAMES[j]][i], i + 2, 2*j)
                self.entries[self.MULTINAMES[j]].append(QtWidgets.QLineEdit())
                self.entries[self.MULTINAMES[j]][i].setAlignment(QtCore.Qt.AlignHCenter)
                self.frame3.addWidget(self.entries[self.MULTINAMES[j]][i], i + 2, 2*j+1)
        self.reset()

    def defaultValues(self, inp):
        if not inp:
            return {'bgrnd':[0.0, True], 'slope':[0.0, True], 'spinspeed':[10.0, True], 't11':np.repeat([np.array([0.0, False], dtype=object)], self.FITNUM, axis=0), 't22':np.repeat([np.array([0.0, False], dtype=object)], self.FITNUM, axis=0), 't33':np.repeat([np.array([0.0, False], dtype=object)], self.FITNUM, axis=0), 'amp':np.repeat([np.array([self.fullInt, False], dtype=object)], self.FITNUM,axis=0), 'lor':np.repeat([np.array([1.0, False], dtype=object)], self.FITNUM,axis=0), 'gauss':np.repeat([np.array([0.0, True], dtype=object)], self.FITNUM,axis=0)}
        else:
            return inp

    def reset(self):
        locList = tuple(self.parent.locList)
        self.parent.pickNum = 0
        self.parent.pickNum2 = 0
        self.cheng = 15
        self.entries['cheng'][-1].setValue(self.cheng)
        self.fitNumList[locList] = 0
        self.fitParamList[locList] = self.defaultValues(0)
        self.pickTick.setChecked(True)
        self.togglePick()
        self.dispParams()
        
    def togglePick(self):
        self.parent.togglePick(self.pickTick.isChecked())

    def changeShiftDef(self):
        NewType = self.entries['shiftdef'][-1].currentIndex()
        OldType = self.shiftDefType 
        if NewType == 0:
            self.label11.show()
            self.label22.show()
            self.label33.show()
            self.labelxx.hide()
            self.labelyy.hide()
            self.labelzz.hide()
            self.labeliso.hide()
            self.labelaniso.hide()
            self.labeleta.hide()
            self.labeliso2.hide()
            self.labelspan.hide()
            self.labelskew.hide()
            self.pickTick.setChecked(True)
            self.pickTick.show()
        elif NewType == 1:
            self.label11.hide()
            self.label22.hide()
            self.label33.hide()
            self.labelxx.show()
            self.labelyy.show()
            self.labelzz.show()
            self.labeliso.hide()
            self.labelaniso.hide()
            self.labeleta.hide()
            self.labeliso2.hide()
            self.labelspan.hide()
            self.labelskew.hide()
            self.pickTick.setChecked(True)
            self.pickTick.show()
        elif NewType == 2:
            self.label11.hide()
            self.label22.hide()
            self.label33.hide()
            self.labelxx.hide()
            self.labelyy.hide()
            self.labelzz.hide()
            self.labeliso.show()
            self.labelaniso.show()
            self.labeleta.show()
            self.labeliso2.hide()
            self.labelspan.hide()
            self.labelskew.hide()
            self.pickTick.setChecked(False)
            self.pickTick.hide()
        elif NewType == 3:
            self.label11.hide()
            self.label22.hide()
            self.label33.hide()
            self.labelxx.hide()
            self.labelyy.hide()
            self.labelzz.hide()
            self.labeliso.hide()
            self.labelaniso.hide()
            self.labeleta.hide()
            self.labeliso2.show()
            self.labelspan.show()
            self.labelskew.show()
            self.pickTick.setChecked(False)
            self.pickTick.hide()
        val = self.numExp.currentIndex() + 1
        tensorList = []
        for i in range(10): #Convert input
            if i < val:
                T11 = safeEval(self.entries['t11'][i].text())
                T22 = safeEval(self.entries['t22'][i].text())
                T33 = safeEval(self.entries['t33'][i].text())
                startTensor = [T11,T22,T33]
                if None in startTensor:
                    self.entries['shiftdef'][-1].setCurrentIndex(OldType) #error, reset to old view
                    self.rootwindow.mainProgram.dispMsg("One of the inputs is not valid")
                    return
                Tensors = shiftConversion(startTensor,OldType)
                for element in range(3): #Check for `ND' s
                    if type(Tensors[NewType][element]) == str:
                        Tensors[NewType][element] = 0
                tensorList.append(Tensors)   
        printStr = '%#.' + str(self.rootwindow.tabWindow.PRECIS) + 'g'
        for i in range(10): #Print output if not stopped before
            if i < val:        
                self.entries['t11'][i].setText(printStr % tensorList[i][NewType][0])
                self.entries['t22'][i].setText(printStr % tensorList[i][NewType][1])
                self.entries['t33'][i].setText(printStr % tensorList[i][NewType][2])
        self.shiftDefType = NewType

    def getExtraParams(self, out):
        out['mas'] = [self.entries['mas'][-1].isChecked()]
        cheng = self.entries['cheng'][-1].value()
        out['cheng'] = [cheng]
        out['shiftdef'] = [self.entries['shiftdef'][0].currentIndex()]
        if out['mas'][0]:
            out['numssb'] = [self.entries['numssb'][0].value()]
            return (out, [out['mas'][-1], out['cheng'][-1], out['shiftdef'][-1], out['numssb'][-1]])
        else:
            phi, theta, weight = zcw_angles(cheng, symm=2)
            out['weight'] = [weight]
            out['multt'] = [[np.sin(theta)**2 * np.cos(phi)**2, np.sin(theta)**2 * np.sin(phi)**2, np.cos(theta)**2]]
            return (out, [out['mas'][-1], out['multt'][-1], out['weight'][-1], out['shiftdef'][-1]])
        
    def disp(self, params, num):
        out = params[num]
        for name in self.SINGLENAMES:
            inp = out[name][0]
            if isinstance(inp, tuple):
                inp = checkLinkTuple(inp)
                out[name][0] = inp[2]*params[inp[4]][inp[0]][inp[1]] + inp[3]
        numExp = len(out[self.MULTINAMES[0]])
        for i in range(numExp):
            for name in self.MULTINAMES:
                inp = out[name][i]
                if isinstance(inp, tuple):
                    inp = checkLinkTuple(inp)
                    out[name][i] = inp[2]*params[inp[4]][inp[0]][inp[1]] + inp[3]
                if not np.isfinite(out[name][i]):
                    self.rootwindow.mainProgram.dispMsg("One of the inputs is not valid")
                    return
        tmpx = self.parent.xax
        outCurveBase = out['bgrnd'][0] + tmpx * out['slope'][0]
        outCurve = outCurveBase.copy()
        outCurvePart = []
        x = []
        for i in range(len(out['amp'])):
            x.append(tmpx)
            if out['mas'][0]:
                y = out['amp'][i] * tensorMASDeconvtensorFunc(tmpx, out['t11'][i] , out['t22'][i], out['t33'][i], out['lor'][i], out['gauss'][i], self.parent.current.sw, self.axAdd, self.axMult, out['spinspeed'][0], out['cheng'][0], out['shiftdef'][-1], out['numssb'][-1])
            else:
                y = out['amp'][i] * tensorDeconvtensorFunc(tmpx, out['t11'][i] , out['t22'][i], out['t33'][i], out['lor'][i], out['gauss'][i], out['multt'][0], self.parent.current.sw, out['weight'][0], self.axAdd, out['shiftdef'][-1], self.axMult)
            outCurvePart.append(outCurveBase + y)
            outCurve += y
        self.parent.fitDataList[tuple(self.parent.locList)] = [tmpx, outCurve, x, outCurvePart]
        self.parent.showFid()

##############################################################################
        
def tensorDeconvmpFit(xax, data1D, guess, args, queue, minmethod):
    try:
        fitVal = scipy.optimize.minimize(lambda *param: np.sum((data1D-tensorDeconvfitFunc(param, xax, args))**2), guess, method=minmethod)
    except:
        fitVal = None
    queue.put(fitVal)

def tensorDeconvfitFunc(params, allX, args):
    params = params[0]
    specName = args[0]
    specSlices = args[1]
    allParam = []
    for length in specSlices:
        allParam.append(params[length])
    allStruc = args[3]
    allArgu = args[4]
    fullTestFunc = []
    for n in range(len(allX)):
        x=allX[n]
        testFunc = np.zeros(len(x))
        param = allParam[n]
        numExp = args[2][n]
        struc = args[3][n]
        argu = args[4][n]
        sw = args[5][n]
        axAdd = args[6][n]
        axMult = args[7][n]
        parameters = {'bgrnd':0.0, 'slope':0.0, 't11':0.0, 't22':0.0, 't33':0.0, 'amp':0.0, 'lor':0.0, 'gauss':0.0}
        mas = argu[-1][0]
        if mas:
            parameters['cheng'] = argu[-1][1]
            parameters['shiftdef'] = argu[-1][2]
            parameters['numssb'] = argu[-1][3]
        else:
            parameters['multt'] = argu[-1][1]
            parameters['weight'] = argu[-1][2]
            parameters['shiftdef'] = argu[-1][3]
        for name in ['spinspeed', 'bgrnd', 'slope']:
            if struc[name][0][0] == 1:
                parameters[name] = param[struc[name][0][1]]
            elif struc[name][0][0] == 0:
                parameters[name] = argu[struc[name][0][1]]
            else:
                altStruc = struc[name][0][1]
                if struc[altStruc[0]][altStruc[1]][0] == 1:
                    parameters[name] = altStruc[2] * allParam[altStruc[4]][struc[altStruc[0]][altStruc[1]][1]] + altStruc[3]
                elif struc[altStruc[0]][altStruc[1]][0] == 0:
                    parameters[name] = altStruc[2] * allArgu[altStruc[4]][struc[altStruc[0]][altStruc[1]][1]] + altStruc[3]
        for i in range(numExp):
            for name in ['t11', 't22', 't33', 'amp', 'lor', 'gauss']:
                if struc[name][i][0] == 1:
                    parameters[name] = param[struc[name][i][1]]
                elif struc[name][i][0] == 0:
                    parameters[name] = argu[struc[name][i][1]]
                else:
                    altStruc = struc[name][i][1]
                    strucTarget = allStruc[altStruc[4]]
                    if strucTarget[altStruc[0]][altStruc[1]][0] == 1:
                        parameters[name] = altStruc[2] * allParam[altStruc[4]][strucTarget[altStruc[0]][altStruc[1]][1]] + altStruc[3]
                    elif strucTarget[altStruc[0]][altStruc[1]][0] == 0:
                        parameters[name] = altStruc[2] * allArgu[altStruc[4]][strucTarget[altStruc[0]][altStruc[1]][1]] + altStruc[3]
            if mas:
                testFunc += parameters['amp'] * tensorMASDeconvtensorFunc(x, parameters['t11'], parameters['t22'], parameters['t33'], parameters['lor'], parameters['gauss'], sw, axAdd, axMult, parameters['spinspeed'], parameters['cheng'], parameters['shiftdef'], parameters['numssb'])
            else:
                testFunc += parameters['amp'] * tensorDeconvtensorFunc(x, parameters['t11'], parameters['t22'], parameters['t33'], parameters['lor'], parameters['gauss'], parameters['multt'], sw, parameters['weight'], axAdd, parameters['shiftdef'], axMult)
        testFunc += parameters['bgrnd'] + parameters['slope'] * x
        fullTestFunc = np.append(fullTestFunc, testFunc)
    return fullTestFunc

def tensorDeconvtensorFunc(x, t11, t22, t33, lor, gauss, multt, sw, weight, axAdd, convention=0, axMult=1):
    if convention == 0 or convention == 1:
        Tensors = shiftConversion([t11/axMult, t22/axMult, t33/axMult], convention)
    else:
        Tensors = shiftConversion([t11/axMult, t22/axMult, t33], convention)
    t11 = Tensors[0][0] * multt[0]
    t22 = Tensors[0][1] * multt[1]
    t33 = Tensors[0][2] * multt[2]
    v = t11 + t22 + t33 - axAdd
    length = len(x)
    t = np.arange(length) / sw
    final = np.zeros(length)
    mult = v / sw * length
    x1 = np.array(np.round(mult) + np.floor(length / 2.0), dtype=int)
    weight = weight[np.logical_and(x1 >= 0, x1 < length)]
    x1 = x1[np.logical_and(x1 >= 0, x1 < length)]
    final = np.bincount(x1, weight, length)
    apod = np.exp(-np.pi * np.abs(lor) * t) * np.exp(-((np.pi * np.abs(gauss) * t)**2) / (4 * np.log(2)))
    apod[-1:int(-(len(apod) / 2 + 1)):-1] = apod[:int(len(apod) / 2)]
    I = np.real(np.fft.fft(np.fft.ifft(final) * apod))
    I = I / sw * len(I)
    return I

def tensorMASDeconvtensorFunc(x, t11, t22, t33, lor, gauss, sw, axAdd, axMult, spinspeed, cheng, convention, numssb):
    if convention == 0 or convention == 1:
        Tensors = shiftConversion([t11/axMult, t22/axMult, t33/axMult], convention)
    else:
        Tensors = shiftConversion([t11/axMult, t22/axMult, t33], convention)
    pos = Tensors[2][0] - axAdd
    delta = Tensors[2][1]
    eta = Tensors[2][2]
    numssb = float(numssb)
    omegar = 2*np.pi*1e3*spinspeed
    phi, theta, weight = zcw_angles(cheng, symm=2)
    sinPhi = np.sin(phi)
    cosPhi = np.cos(phi)
    sin2Theta = np.sin(2 * theta)
    cos2Theta = np.cos(2 * theta)
    tresolution = 2 * np.pi / omegar / numssb
    t = np.linspace(0, tresolution * (numssb - 1), numssb)
    cosOmegarT = np.cos(omegar * t)
    cos2OmegarT = np.cos(2 * omegar * t)
    angleStuff = [np.array([np.sqrt(2) / 3 * sinPhi * cosPhi * 3]).transpose() * cosOmegarT,
                  np.array([-1.0 / 3 * 3 / 2 * sinPhi**2]).transpose() * cos2OmegarT,
                  np.transpose([cos2Theta / 3.0]) * (np.array([np.sqrt(2) / 3 * sinPhi * cosPhi * 3]).transpose() * cosOmegarT),
                  np.array([1.0 / 3 / 2 * (1 + cosPhi**2) * cos2Theta]).transpose() * cos2OmegarT,
                  np.array([np.sqrt(2) / 3 * sinPhi * sin2Theta]).transpose() * np.sin(omegar * t),
                  np.array([cosPhi * sin2Theta / 3]).transpose() * np.sin(2 * omegar * t)]
    omegars = 2 * np.pi * delta * (angleStuff[0] + angleStuff[1] + eta * (angleStuff[2] + angleStuff[3] + angleStuff[4] + angleStuff[5]))
    numssb = angleStuff[0].shape[1]
    QTrs = np.concatenate([np.ones([angleStuff[0].shape[0], 1]), np.exp(-1j * np.cumsum(omegars, axis=1) * tresolution)[:, :-1]], 1)
    for j in range(1, numssb):
        QTrs[:, j] = np.exp(-1j * np.sum(omegars[:, 0:j] * tresolution, 1))
    rhoT0sr = np.conj(QTrs)
    # calculate the gamma-averaged FID over 1 rotor period for all crystallites
    favrs = np.zeros(numssb, dtype=complex)
    for j in range(numssb):
        favrs[j] += np.sum(weight * np.sum(rhoT0sr * np.roll(QTrs, -j, axis=1), 1) / numssb**2)
    # calculate the sideband intensities by doing an FT and pick the ones that are needed further
    inten = np.real(np.fft.fft(favrs))
    posList = np.array(np.fft.fftfreq(numssb, 1.0/numssb))*spinspeed*1e3 + pos
    length = len(x)
    t = np.arange(length) / sw
    mult = posList / sw * length
    x1 = np.array(np.round(mult) + np.floor(length / 2), dtype=int)
    weights = inten[np.logical_and(x1 >= 0, x1 < length)]
    x1 = x1[np.logical_and(x1 >= 0, x1 < length)]
    final = np.bincount(x1, weights, length)
    apod = np.exp(-np.pi * np.abs(lor) * t) * np.exp(-((np.pi * np.abs(gauss) * t)**2) / (4 * np.log(2)))
    apod[-1:-(int(len(apod) / 2) + 1):-1] = apod[:int(len(apod) / 2)]
    inten = np.real(np.fft.fft(np.fft.ifft(final) * apod))
    inten = inten / sw * len(inten)
    return inten

##############################################################################


class Quad1DeconvWindow(TabFittingWindow):

    def __init__(self, mainProgram, oldMainWindow):
        self.CURRENTWINDOW = Quad1DeconvFrame
        self.PARAMFRAME = Quad1DeconvParamFrame
        super(Quad1DeconvWindow, self).__init__(mainProgram, oldMainWindow)

#################################################################################


class Quad1DeconvFrame(FitPlotFrame):

    def __init__(self, rootwindow, fig, canvas, current):
        self.FITNUM = 10 # Maximum number of fits
        super(Quad1DeconvFrame, self).__init__(rootwindow, fig, canvas, current)

#################################################################################


class Quad1DeconvParamFrame(AbstractParamFrame):
    
    Ioptions = ['1', '3/2', '2', '5/2', '3', '7/2', '4', '9/2']
    Ivalues = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5]

    def __init__(self, parent, rootwindow, isMain=True):
        self.SINGLENAMES = ['bgrnd', 'slope', 'spinspeed']
        self.MULTINAMES = ['pos', 'cq', 'eta', 'amp', 'lor', 'gauss']
        self.FITFUNC = quad1mpFit
        self.setAngleStuff = quad1DeconvsetAngleStuff
        self.tensorFunc = quad1DeconvtensorFunc
        self.cheng = 15
        #Get full integral
        self.fullInt = np.sum(parent.data1D) * parent.current.sw / float(len(parent.data1D))
        super(Quad1DeconvParamFrame, self).__init__(parent, rootwindow, isMain)
        self.ticks = {'bgrnd':[], 'slope':[], 'spinspeed':[], 'pos':[], 'cq':[], 'eta':[], 'amp':[], 'lor':[], 'gauss':[]}
        self.entries = {'bgrnd':[], 'slope':[], 'spinspeed':[], 'pos':[], 'cq':[], 'eta':[], 'amp':[], 'lor':[], 'gauss':[], 'shiftdef':[], 'cheng':[], 'I':[], 'mas':[], 'numssb':[]}
        self.optframe.addWidget(QLabel("Cheng:"), 0, 0)
        self.entries['cheng'].append(QtWidgets.QSpinBox())
        self.entries['cheng'][-1].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['cheng'][-1].setValue(self.cheng)
        self.optframe.addWidget(self.entries['cheng'][-1], 1, 0)
        self.optframe.addWidget(QLabel("I:"), 2, 0)
        self.entries['I'].append(QtWidgets.QComboBox())
        self.entries['I'][-1].addItems(self.Ioptions)
        self.entries['I'][-1].setCurrentIndex(1)
        self.optframe.addWidget(self.entries['I'][-1], 3, 0)
        self.entries['mas'].append(QtWidgets.QCheckBox('Spinning'))
        self.optframe.addWidget(self.entries['mas'][-1], 4, 0)
        self.optframe.addWidget(QLabel("# sidebands:"), 5, 0)
        self.entries['numssb'].append(QtWidgets.QSpinBox())
        self.entries['numssb'][-1].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['numssb'][-1].setValue(32)
        self.optframe.addWidget(self.entries['numssb'][-1], 6, 0)
        self.optframe.setColumnStretch(10, 1)
        self.optframe.setAlignment(QtCore.Qt.AlignTop)
        self.spinLabel = QLabel("Spin. speed [kHz]:")
        self.frame2.addWidget(self.spinLabel, 0, 0, 1, 2)
        self.ticks['spinspeed'].append(QtWidgets.QCheckBox(''))
        self.frame2.addWidget(self.ticks['spinspeed'][-1], 1, 0)
        self.entries['spinspeed'].append(QtWidgets.QLineEdit())
        self.entries['spinspeed'][-1].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['spinspeed'][-1].setText("10.0")
        self.frame2.addWidget(self.entries['spinspeed'][-1], 1, 1)
        self.frame2.addWidget(QLabel("Bgrnd:"), 2, 0, 1, 2)
        self.ticks['bgrnd'].append(QtWidgets.QCheckBox(''))
        self.frame2.addWidget(self.ticks['bgrnd'][-1], 3, 0)
        self.entries['bgrnd'].append(QtWidgets.QLineEdit())
        self.entries['bgrnd'][-1].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['bgrnd'][-1].setText("0.0")
        self.frame2.addWidget(self.entries['bgrnd'][-1], 3, 1)
        self.frame2.addWidget(QLabel("Slope:"), 4, 0, 1, 2)
        self.ticks['slope'].append(QtWidgets.QCheckBox(''))
        self.frame2.addWidget(self.ticks['slope'][-1], 5, 0)
        self.entries['slope'].append(QtWidgets.QLineEdit())
        self.entries['slope'][-1].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['slope'][-1].setText("0.0")
        self.frame2.addWidget(self.entries['slope'][-1], 5, 1)
        self.frame2.setColumnStretch(10, 1)
        self.frame2.setAlignment(QtCore.Qt.AlignTop)
        self.numExp = QtWidgets.QComboBox()
        self.numExp.addItems([str(x+1) for x in range(self.FITNUM)])
        self.numExp.currentIndexChanged.connect(self.changeNum)
        self.frame3.addWidget(self.numExp, 0, 0, 1, 2)
        if self.parent.current.ppm:
            axUnit = 'ppm'
        else:
            axUnit = ['Hz', 'kHz', 'MHz'][self.parent.current.axType]
        #Labels
        self.labelpos = QLabel(u'Position [' + axUnit + ']:')
        self.labelcq = QLabel(u'C<sub>Q</sub> [MHz]:')
        self.labeleta = QLabel(u'\u03B7:')
        self.frame3.addWidget(self.labelpos, 1, 0, 1, 2)
        self.frame3.addWidget(self.labelcq, 1, 2, 1, 2)
        self.frame3.addWidget(self.labeleta, 1, 4, 1, 2)
        self.frame3.addWidget(QLabel("Integral:"), 1, 6, 1, 2)
        self.frame3.addWidget(QLabel("Lorentz [Hz]:"), 1, 8, 1, 2)
        self.frame3.addWidget(QLabel("Gauss [Hz]:"), 1, 10, 1, 2)
        self.frame3.setColumnStretch(20, 1)
        self.frame3.setAlignment(QtCore.Qt.AlignTop)
        for i in range(self.FITNUM):
            for j in range(len(self.MULTINAMES)):
                self.ticks[self.MULTINAMES[j]].append(QtWidgets.QCheckBox(''))
                self.frame3.addWidget(self.ticks[self.MULTINAMES[j]][i], i + 2, 2*j)
                self.entries[self.MULTINAMES[j]].append(QtWidgets.QLineEdit())
                self.entries[self.MULTINAMES[j]][i].setAlignment(QtCore.Qt.AlignHCenter)
                self.frame3.addWidget(self.entries[self.MULTINAMES[j]][i], i + 2, 2*j+1)
        self.dispParams()

    def defaultValues(self, inp):
        if not inp:
            return {'bgrnd':[0.0, True], 'slope':[0.0, True], 'spinspeed':[10.0, True], 'pos':np.repeat([np.array([0.0, False], dtype=object)], self.FITNUM, axis=0), 'cq':np.repeat([np.array([1.0, False], dtype=object)], self.FITNUM, axis=0), 'eta':np.repeat([np.array([0.0, False], dtype=object)], self.FITNUM, axis=0), 'amp':np.repeat([np.array([self.fullInt, False], dtype=object)], self.FITNUM,axis=0), 'lor':np.repeat([np.array([1.0, False], dtype=object)], self.FITNUM,axis=0), 'gauss':np.repeat([np.array([0.0, True], dtype=object)], self.FITNUM,axis=0)}
        else:
            return inp

    def checkI(self, I):
        return I * 0.5 + 1

    def getExtraParams(self, out):
        out['mas'] = [self.entries['mas'][-1].isChecked()]
        out['I'] = [self.checkI(self.entries['I'][-1].currentIndex())]
        cheng = safeEval(self.entries['cheng'][-1].text())
        out['cheng'] = [cheng]
        if out['mas'][0]:
            out['numssb'] = [self.entries['numssb'][0].value()]
            return (out, [out['mas'][-1], out['I'][-1], out['cheng'][-1], out['numssb'][-1]])
        else:
            weight, angleStuff = self.setAngleStuff(cheng)
            out['weight'] = [weight]
            out['anglestuff'] = [angleStuff]
            out['tensorfunc'] = [self.tensorFunc]
            out['freq'] = [self.parent.current.freq]
            return (out, [out['mas'][-1], out['I'][-1], out['weight'][-1], out['anglestuff'][-1], out['tensorfunc'][-1], out['freq'][-1]])

    def disp(self, params, num):
        out = params[num]
        for name in self.SINGLENAMES:
            inp = out[name][0]
            if isinstance(inp, tuple):
                inp = checkLinkTuple(inp)
                out[name][0] = inp[2]*params[inp[4]][inp[0]][inp[1]] + inp[3]
        numExp = len(out[self.MULTINAMES[0]])
        for i in range(numExp):
            for name in self.MULTINAMES:
                inp = out[name][i]
                if isinstance(inp, tuple):
                    inp = checkLinkTuple(inp)
                    out[name][i] = inp[2]*params[inp[4]][inp[0]][inp[1]] + inp[3]
                if not np.isfinite(out[name][i]):
                    self.rootwindow.mainProgram.dispMsg("One of the inputs is not valid")
                    return
        tmpx = self.parent.xax
        outCurveBase = out['bgrnd'][0] + tmpx * out['slope'][0]
        outCurve = outCurveBase.copy()
        outCurvePart = []
        x = []
        for i in range(len(out['amp'])):
            x.append(tmpx)
            if out['mas'][0]:
                y = out['amp'][i] * quad1MASFunc(tmpx, out['pos'][i] , out['cq'][i], out['eta'][i], out['lor'][i], out['gauss'][i], self.parent.current.sw, self.axAdd, self.axMult, out['spinspeed'][0], out['cheng'][0], out['I'][0], out['numssb'][0])
            else:
                y = out['amp'][i] * self.tensorFunc(tmpx, out['I'][0], out['pos'][i], out['cq'][i], out['eta'][i], out['lor'][i], out['gauss'][i], out['anglestuff'][0], self.parent.current.freq, self.parent.current.sw, out['weight'][0], self.axAdd, self.axMult)
            outCurvePart.append(outCurveBase + y)
            outCurve += y
        self.parent.fitDataList[tuple(self.parent.locList)] = [tmpx, outCurve, x, outCurvePart]
        self.parent.showFid()

##############################################################################

def quad1mpFit(xax, data1D, guess, args, queue, minmethod):
    try:
        fitVal = scipy.optimize.minimize(lambda *param: np.sum((data1D-quad1fitFunc(param, xax, args))**2), guess, method=minmethod)
    except:
        fitVal = None
    queue.put(fitVal)

def quad1fitFunc(params, allX, args):
    params = params[0]
    specName = args[0]
    specSlices = args[1]
    allParam = []
    for length in specSlices:
        allParam.append(params[length])
    allStruc = args[3]
    allArgu = args[4]
    fullTestFunc = []
    for n in range(len(allX)):
        x=allX[n]
        testFunc = np.zeros(len(x))
        param = allParam[n]
        numExp = args[2][n]
        struc = args[3][n]
        argu = args[4][n]
        sw = args[5][n]
        axAdd = args[6][n]
        axMult = args[7][n]
        parameters = {'spinspeed':0.0, 'bgrnd':0.0, 'slope':0.0, 'pos':0.0, 'cq':0.0, 'eta':0.0, 'amp':0.0, 'lor':0.0, 'gauss':0.0}
        mas = argu[-1][0]
        parameters['I'] = argu[-1][1]
        if mas:
            parameters['cheng'] = argu[-1][2]
            parameters['numssb'] = argu[-1][3]
        else:
            parameters['weight'] = argu[-1][2]
            parameters['anglestuff'] = argu[-1][3]
            parameters['tensorfunc'] = argu[-1][4]
            parameters['freq'] = argu[-1][5]
        for name in ['spinspeed', 'bgrnd', 'slope']:
            if struc[name][0][0] == 1:
                parameters[name] = param[struc[name][0][1]]
            elif struc[name][0][0] == 0:
                parameters[name] = argu[struc[name][0][1]]
            else:
                altStruc = struc[name][0][1]
                if struc[altStruc[0]][altStruc[1]][0] == 1:
                    parameters[name] = altStruc[2] * allParam[altStruc[4]][struc[altStruc[0]][altStruc[1]][1]] + altStruc[3]
                elif struc[altStruc[0]][altStruc[1]][0] == 0:
                    parameters[name] = altStruc[2] * allArgu[altStruc[4]][struc[altStruc[0]][altStruc[1]][1]] + altStruc[3]
        for i in range(numExp):
            for name in ['pos', 'cq', 'eta', 'amp', 'lor', 'gauss']:
                if struc[name][i][0] == 1:
                    parameters[name] = param[struc[name][i][1]]
                elif struc[name][i][0] == 0:
                    parameters[name] = argu[struc[name][i][1]]
                else:
                    altStruc = struc[name][i][1]
                    strucTarget = allStruc[altStruc[4]]
                    if strucTarget[altStruc[0]][altStruc[1]][0] == 1:
                        parameters[name] = altStruc[2] * allParam[altStruc[4]][strucTarget[altStruc[0]][altStruc[1]][1]] + altStruc[3]
                    elif strucTarget[altStruc[0]][altStruc[1]][0] == 0:
                        parameters[name] = altStruc[2] * allArgu[altStruc[4]][strucTarget[altStruc[0]][altStruc[1]][1]] + altStruc[3]
            if mas:
                testFunc += parameters['amp'] * quad1MASFunc(x, parameters['pos'], parameters['cq'], parameters['eta'], parameters['lor'], parameters['gauss'], sw, axAdd, axMult, parameters['spinspeed'], parameters['cheng'], parameters['I'], parameters['numssb'])
            else:
                testFunc += parameters['amp'] * parameters['tensorfunc'](x, parameters['I'], parameters['pos'], parameters['cq'], parameters['eta'], parameters['lor'], parameters['gauss'], parameters['anglestuff'], parameters['freq'], sw, parameters['weight'], axAdd, axMult)
            testFunc += parameters['bgrnd'] + parameters['slope'] * x
        fullTestFunc = np.append(fullTestFunc, testFunc)
    return fullTestFunc

def quad1DeconvtensorFunc(x, I, pos, cq, eta, width, gauss, angleStuff, freq, sw, weight, axAdd, axMult = 1):
    m = np.arange(-I, I)
    v = []
    cq *= 1e6
    weights = []
    pos = (pos / axMult)- axAdd
    for i in m:
        tmp = (cq / (4 * I * (2 * I - 1)) * (I * (I + 1) - 3 * (i + 1)**2)) - (cq / (4 * I * (2 * I - 1)) * (I * (I + 1) - 3 * (i)**2))
        v = np.append(v, tmp * (angleStuff[0] - eta * angleStuff[1]) + pos)
        weights = np.append(weights, weight)
    length = len(x)
    t = np.arange(length) / sw
    final = np.zeros(length)
    mult = v / sw * length
    x1 = np.array(np.round(mult) + np.floor(length / 2), dtype=int)
    weights = weights[np.logical_and(x1 >= 0, x1 < length)]
    x1 = x1[np.logical_and(x1 >= 0, x1 < length)]
    final = np.bincount(x1, weights, length)
    apod = np.exp(-np.pi * np.abs(width) * t) * np.exp(-((np.pi * np.abs(gauss) * t)**2) / (4 * np.log(2)))
    apod[-1:-int(len(apod) / 2 + 1):-1] = apod[:int(len(apod) / 2)]
    inten = np.real(np.fft.fft(np.fft.ifft(final) * apod))
    inten = inten / sw * len(inten) / (2 * I)
    return inten

def quad1DeconvsetAngleStuff(cheng):
    phi, theta, weight = zcw_angles(cheng, symm=2)
    angleStuff = [0.5 * (3 * np.cos(theta)**2 - 1), 0.5 * np.cos(2 * phi) * (np.sin(theta)**2)]
    return weight, angleStuff

def quad1MASFunc(x, pos, cq, eta, lor, gauss, sw, axAdd, axMult, spinspeed, cheng, I, numssb):
    numssb = float(numssb)
    omegar = 2*np.pi*1e3*spinspeed
    phi, theta, weight = zcw_angles(cheng, symm=2)
    sinPhi = np.sin(phi)
    cosPhi = np.cos(phi)
    sin2Theta = np.sin(2 * theta)
    cos2Theta = np.cos(2 * theta)
    tresolution = 2 * np.pi / omegar / numssb
    t = np.linspace(0, tresolution * (numssb - 1), int(numssb))
    cosOmegarT = np.cos(omegar * t)
    cos2OmegarT = np.cos(2 * omegar * t)
    angleStuff = [np.array([np.sqrt(2) / 3 * sinPhi * cosPhi * 3]).transpose() * cosOmegarT,
                  np.array([-1.0 / 3 * 3 / 2 * sinPhi**2]).transpose() * cos2OmegarT,
                  np.transpose([cos2Theta / 3.0]) * (np.array([np.sqrt(2) / 3 * sinPhi * cosPhi * 3]).transpose() * cosOmegarT),
                  np.array([1.0 / 3 / 2 * (1 + cosPhi**2) * cos2Theta]).transpose() * cos2OmegarT,
                  np.array([np.sqrt(2) / 3 * sinPhi * sin2Theta]).transpose() * np.sin(omegar * t),
                  np.array([cosPhi * sin2Theta / 3]).transpose() * np.sin(2 * omegar * t)]
    pos = (pos / axMult)- axAdd
    m = np.arange(-I, 0)  # Only half the transitions have to be caclulated, as the others are mirror images (sidebands inversed)
    eff = I**2 + I - m * (m + 1)  # The detection efficiencies of the top half transitions
    splitting = np.arange(I - 0.5, -0.1, -1) # The quadrupolar couplings of the top half transitions
    sidebands = np.zeros(int(numssb))
    for transition in range(len(eff)):  # For all transitions
        if splitting[transition] != 0:  # If quad coupling not zero: calculate sideban pattern
            delta = splitting[transition] * 2 * np.pi * 3 / (2 * I * (2 * I - 1)) * cq * 1e6  # Calc delta based on Cq [MHz] and spin qunatum
            omegars = delta * (angleStuff[0] + angleStuff[1] + eta * (angleStuff[2] + angleStuff[3] + angleStuff[4] + angleStuff[5]))
            QTrs = np.concatenate([np.ones([angleStuff[0].shape[0], 1]), np.exp(-1j * np.cumsum(omegars, axis=1) * tresolution)[:, :-1]], 1)
            for j in range(1, int(numssb)):
                QTrs[:, j] = np.exp(-1j * np.sum(omegars[:, 0:j] * tresolution, 1))
            rhoT0sr = np.conj(QTrs)
            # calculate the gamma-averaged FID over 1 rotor period for all crystallites
            favrs = np.zeros(int(numssb), dtype=complex)
            for j in range(int(numssb)):
                favrs[j] += np.sum(weight * np.sum(rhoT0sr * np.roll(QTrs, -j, axis=1), 1) / numssb**2)
            # calculate the sideband intensities by doing an FT and pick the ones that are needed further
            partbands = np.real(np.fft.fft(favrs))
            sidebands += eff[transition] * (partbands + np.roll(np.flipud(partbands), 1))
        else:  # If zero: add all the intensity to the centreband
            sidebands[0] += eff[transition]
    posList = np.array(np.fft.fftfreq(int(numssb), 1.0/numssb))*spinspeed*1e3 + pos
    length = len(x)
    t = np.arange(length) / sw
    mult = posList / sw * length
    x1 = np.array(np.round(mult) + np.floor(length / 2), dtype=int)
    weights = sidebands[np.logical_and(x1 >= 0, x1 < length)]
    x1 = x1[np.logical_and(x1 >= 0, x1 < length)]
    final = np.bincount(x1, weights, length)
    apod = np.exp(-np.pi * np.abs(lor) * t) * np.exp(-((np.pi * np.abs(gauss) * t)**2) / (4 * np.log(2)))
    apod[-1:-(int(len(apod) / 2) + 1):-1] = apod[:int(len(apod) / 2)]
    inten = np.real(np.fft.fft(np.fft.ifft(final) * apod))
    inten = inten / sw * len(inten)
    return inten

##############################################################################


class Quad2DeconvWindow(TabFittingWindow):

    def __init__(self, mainProgram, oldMainWindow):
        self.CURRENTWINDOW = Quad1DeconvFrame
        self.PARAMFRAME = Quad2DeconvParamFrame
        super(Quad2DeconvWindow, self).__init__(mainProgram, oldMainWindow)

#################################################################################


class Quad2DeconvParamFrame(Quad1DeconvParamFrame):

    Ioptions = ['3/2', '5/2', '7/2', '9/2']
    def __init__(self, parent, rootwindow, isMain=True):
        super(Quad2DeconvParamFrame, self).__init__(parent, rootwindow, isMain)
        self.setAngleStuff = quad2StaticsetAngleStuff
        self.tensorFunc = quad2tensorFunc
        self.entries['I'][-1].setCurrentIndex(0)
        self.spinLabel.hide()
        self.ticks['spinspeed'][-1].hide()
        self.entries['spinspeed'][-1].hide()

    def getExtraParams(self, out):
        out['mas'] = [0] # Second order quadrupole MAS is calculated as if it is static
        if self.entries['mas'][-1].isChecked():
            self.setAngleStuff = quad2MASsetAngleStuff
        else:
            self.setAngleStuff = quad2StaticsetAngleStuff
        out['I'] = [self.checkI(self.entries['I'][-1].currentIndex())]
        cheng = safeEval(self.entries['cheng'][-1].text())
        out['cheng'] = [cheng]
        weight, angleStuff = self.setAngleStuff(cheng)
        out['weight'] = [weight]
        out['anglestuff'] = [angleStuff]
        out['tensorfunc'] = [self.tensorFunc]
        out['freq'] = [self.parent.current.freq]
        return (out, [out['mas'][-1], out['I'][-1], out['weight'][-1], out['anglestuff'][-1], out['tensorfunc'][-1], out['freq'][-1]])
    
    def checkI(self, I):
        return I * 1.0 + 1.5

##############################################################################

def quad2tensorFunc(x, I, pos, cq, eta, width, gauss, angleStuff, freq, sw, weight, axAdd, axMult = 1):
    pos = (pos / axMult)- axAdd
    cq *= 1e6
    v = -1 / (6 * freq) * (3 * cq / (2 * I * (2 * I - 1)))**2 * (I * (I + 1) - 3.0 / 4) * (angleStuff[0] + angleStuff[1] * eta + angleStuff[2] * eta**2) + pos
    length = len(x)
    t = np.arange(length) / sw
    final = np.zeros(length)
    mult = v / sw * length
    x1 = np.array(np.round(mult) + np.floor(length / 2), dtype=int)
    weights = weight[np.logical_and(x1 >= 0, x1 < length)]
    x1 = x1[np.logical_and(x1 >= 0, x1 < length)]
    final = np.bincount(x1, weights, length)
    apod = np.exp(-np.pi * np.abs(width) * t) * np.exp(-((np.pi * np.abs(gauss) * t)**2) / (4 * np.log(2)))
    apod[-1:-(int(len(apod) / 2) + 1):-1] = apod[:int(len(apod) / 2)]
    inten = np.real(np.fft.fft(np.fft.ifft(final) * apod))
    inten = inten / sw * len(inten)
    return inten

def quad2StaticsetAngleStuff(cheng):
    phi, theta, weight = zcw_angles(cheng, symm=2)
    angleStuff = [-27 / 8.0 * np.cos(theta)**4 + 15 / 4.0 * np.cos(theta)**2 - 3 / 8.0,
                  (-9 / 4.0 * np.cos(theta)**4 + 2 * np.cos(theta)**2 + 1 / 4.0) * np.cos(2 * phi),
                  -1 / 2.0 * np.cos(theta)**2 + 1 / 3.0 + (-3 / 8.0 * np.cos(theta)**4 + 3 / 4.0 * np.cos(theta)**2 - 3 / 8.0) * np.cos(2 * phi)**2]
    return weight, angleStuff

def quad2MASsetAngleStuff(cheng):
    phi, theta, weight = zcw_angles(cheng, symm=2)
    angleStuff = [21 / 16.0 * np.cos(theta)**4 - 9 / 8.0 * np.cos(theta)**2 + 5 / 16.0,
                  (-7 / 8.0 * np.cos(theta)**4 + np.cos(theta)**2 - 1 / 8.0) * np.cos(2 * phi),
                  1 / 12.0 * np.cos(theta)**2 + (+7 / 48.0 * np.cos(theta)**4 - 7 / 24.0 * np.cos(theta)**2 + 7 / 48.0) * np.cos(2 * phi)**2]
    return weight, angleStuff

##############################################################################


class Quad2CzjzekWindow(TabFittingWindow):

    def __init__(self, mainProgram, oldMainWindow):
        self.CURRENTWINDOW = Quad1DeconvFrame
        self.PARAMFRAME = Quad2CzjzekParamFrame
        super(Quad2CzjzekWindow, self).__init__(mainProgram, oldMainWindow)

#################################################################################


class Quad2CzjzekParamFrame(AbstractParamFrame):

    Ioptions = ['3/2', '5/2', '7/2', '9/2']
    
    def __init__(self, parent, rootwindow, isMain=True):
        self.SINGLENAMES = ['bgrnd', 'slope']
        self.MULTINAMES = ['d', 'pos', 'sigma', 'amp', 'lor', 'gauss']
        self.FITFUNC = quad2CzjzekmpFit
        #Get full integral
        self.fullInt = np.sum(parent.data1D) * parent.current.sw / float(len(parent.data1D))
        super(Quad2CzjzekParamFrame, self).__init__(parent, rootwindow, isMain)
        self.cheng = 15
        if self.parent.current.spec == 1:
            self.axAdd = self.parent.current.freq - self.parent.current.ref
        elif self.parent.current.spec == 0:
            self.axAdd = 0
        self.ticks = {'bgrnd':[], 'slope':[], 'pos':[], 'd':[], 'sigma':[], 'amp':[], 'lor':[], 'gauss':[]}
        self.entries = {'bgrnd':[], 'slope':[], 'pos':[], 'd':[], 'sigma':[], 'amp':[], 'lor':[], 'gauss':[], 'method':[], 'cheng':[], 'I':[], 'wqgrid':[], 'etagrid':[], 'wqmax':[], 'mas':[]}
        self.optframe.addWidget(QLabel("Cheng:"), 0, 0)
        self.entries['cheng'].append(QtWidgets.QSpinBox())
        self.entries['cheng'][-1].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['cheng'][-1].setValue(self.cheng)
        self.optframe.addWidget(self.entries['cheng'][-1], 1, 0)
        self.optframe.addWidget(QLabel("I:"), 2, 0)
        self.entries['I'].append(QtWidgets.QComboBox())
        self.entries['I'][-1].addItems(self.Ioptions)
        self.entries['I'][-1].setCurrentIndex(1)
        self.optframe.addWidget(self.entries['I'][-1], 3, 0)
        self.entries['mas'].append(QtWidgets.QCheckBox('Spinning'))
        self.optframe.addWidget(self.entries['mas'][-1], 4, 0)
        self.optframe.addWidget(QLabel(u"\u03c9<sub>Q</sub> grid size:"), 5, 0)
        self.entries['wqgrid'].append(QtWidgets.QLineEdit())
        self.entries['wqgrid'][-1].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['wqgrid'][-1].setText("50")
        self.entries['wqgrid'][-1].returnPressed.connect(self.setGrid)
        self.optframe.addWidget(self.entries['wqgrid'][-1], 6, 0)
        self.optframe.addWidget(QLabel(u"\u03b7 grid size:"), 7, 0)
        self.entries['etagrid'].append(QtWidgets.QLineEdit())
        self.entries['etagrid'][-1].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['etagrid'][-1].setText("10")
        self.entries['etagrid'][-1].returnPressed.connect(self.setGrid)
        self.optframe.addWidget(self.entries['etagrid'][-1], 8, 0)
        self.optframe.addWidget(QLabel(u"\u03c9<sub>Q</sub><sup>max</sup>/\u03c3:"), 9, 0)
        self.entries['wqmax'].append(QtWidgets.QLineEdit())
        self.entries['wqmax'][-1].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['wqmax'][-1].setText("4")
        self.entries['wqmax'][-1].returnPressed.connect(self.setGrid)
        self.optframe.addWidget(self.entries['wqmax'][-1], 10, 0)
        self.optframe.setColumnStretch(11, 1)
        self.optframe.setAlignment(QtCore.Qt.AlignTop)    
        self.frame2.addWidget(QLabel("Bgrnd:"), 0, 0, 1, 2)
        self.ticks['bgrnd'].append(QtWidgets.QCheckBox(''))
        self.ticks['bgrnd'][-1].setChecked(True)
        self.frame2.addWidget(self.ticks['bgrnd'][-1], 1, 0)
        self.entries['bgrnd'].append(QtWidgets.QLineEdit())
        self.entries['bgrnd'][-1].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['bgrnd'][-1].setText("0.0")
        self.frame2.addWidget(self.entries['bgrnd'][-1], 1, 1)
        self.frame2.addWidget(QLabel("Slope:"), 2, 0, 1, 2)
        self.ticks['slope'].append(QtWidgets.QCheckBox(''))
        self.ticks['slope'][-1].setChecked(True)
        self.frame2.addWidget(self.ticks['slope'][-1], 3, 0)
        self.entries['slope'].append(QtWidgets.QLineEdit())
        self.entries['slope'][-1].setAlignment(QtCore.Qt.AlignHCenter)
        self.entries['slope'][-1].setText("0.0")
        self.frame2.addWidget(self.entries['slope'][-1], 3, 1)
        self.frame2.setColumnStretch(10, 1)
        self.frame2.setAlignment(QtCore.Qt.AlignTop)
        self.numExp = QtWidgets.QComboBox()
        self.numExp.addItems([str(x+1) for x in range(self.FITNUM)])
        self.numExp.currentIndexChanged.connect(self.changeNum)
        self.frame3.addWidget(self.numExp, 0, 0, 1, 2)
        self.frame3.addWidget(QLabel("d:"), 1, 0, 1, 2)
        if self.parent.current.ppm:
            axUnit = 'ppm'
        else:
            axUnit = ['Hz', 'kHz', 'MHz'][self.parent.current.axType]
        self.frame3.addWidget(QLabel("Pos [" + axUnit + "]:"), 1, 2, 1, 2)
        self.frame3.addWidget(QLabel(u"\u03c3 [MHz]:"), 1, 4, 1, 2)
        self.frame3.addWidget(QLabel("Integral:"), 1, 6, 1, 2)
        self.frame3.addWidget(QLabel("Lorentz [Hz]:"), 1, 8, 1, 2)
        self.frame3.addWidget(QLabel("Gauss [Hz]:"), 1, 10, 1, 2)
        self.frame3.setColumnStretch(20, 1)
        self.frame3.setAlignment(QtCore.Qt.AlignTop)
        for i in range(self.FITNUM):
            for j in range(len(self.MULTINAMES)):
                self.ticks[self.MULTINAMES[j]].append(QtWidgets.QCheckBox(''))
                self.frame3.addWidget(self.ticks[self.MULTINAMES[j]][i], i + 2, 2*j)
                self.entries[self.MULTINAMES[j]].append(QtWidgets.QLineEdit())
                self.entries[self.MULTINAMES[j]][i].setAlignment(QtCore.Qt.AlignHCenter)
                self.frame3.addWidget(self.entries[self.MULTINAMES[j]][i], i + 2, 2*j+1)
        self.dispParams()

    def defaultValues(self, inp):
        if not inp:
            return {'bgrnd':[0.0, True], 'slope':[0.0, True], 'pos':np.repeat([np.array([0.0, False], dtype=object)], self.FITNUM, axis=0), 'd':np.repeat([np.array([5.0, False], dtype=object)], self.FITNUM, axis=0), 'sigma':np.repeat([np.array([1.0, False], dtype=object)], self.FITNUM, axis=0), 'amp':np.repeat([np.array([self.fullInt, False], dtype=object)], self.FITNUM,axis=0), 'lor':np.repeat([np.array([10.0, False], dtype=object)], self.FITNUM,axis=0), 'gauss':np.repeat([np.array([0.0, True], dtype=object)], self.FITNUM,axis=0)}
        else:
            return inp

    def checkI(self, I):
        return I * 1.0 + 1.5

    def setGrid(self, *args):
        inp = safeEval(self.entries['wqgrid'][-1].text())
        if inp is None:
            return False
        self.entries['wqgrid'][-1].setText(str(int(inp)))
        inp = safeEval(self.entries['etagrid'][-1].text())
        if inp is None:
            return False
        self.entries['etagrid'][-1].setText(str(int(inp)))
        inp = safeEval(self.entries['wqmax'][-1].text())
        if inp is None:
            return False
        self.entries['wqmax'][-1].setText(str(float(inp)))
        return True

    def bincounting(self, x1, weight, length):
        weights = weight[np.logical_and(x1 >= 0, x1 < length)]
        x1 = x1[np.logical_and(x1 >= 0, x1 < length)]
        return np.fft.ifft(np.bincount(x1, weights, length))

    def genLib(self, length, I, maxWq, numWq, numEta, angleStuff, freq, sw, weight, axAdd):
        wq_return, eta_return = np.meshgrid(np.linspace(0, maxWq, numWq), np.linspace(0, 1, numEta))
        wq = wq_return[..., None]
        eta = eta_return[..., None]
        v = -1 / (6.0 * freq) * wq**2 * (I * (I + 1) - 3.0 / 4) * (angleStuff[0] + angleStuff[1] * eta + angleStuff[2] * eta**2)
        mult = v / sw * length
        x1 = np.array(np.round(mult) + np.floor(length / 2), dtype=int)
        lib = np.apply_along_axis(self.bincounting, 2, x1, weight, length)
        return lib, wq_return, eta_return

    def getExtraParams(self, out):
        mas = self.entries['mas'][-1].isChecked()
        wqMax = safeEval(self.entries['wqmax'][-1].text())
        I = self.checkI(self.entries['I'][-1].currentIndex())
        numWq = int(self.entries['wqgrid'][-1].text())
        numEta = int(self.entries['etagrid'][-1].text())
        if mas:
            weight, angleStuff = czjzekMASsetAngleStuff(self.entries['cheng'][-1].value())
        else:
            weight, angleStuff = czjzekStaticsetAngleStuff(self.entries['cheng'][-1].value())
        maxSigma = max(out['sigma'])
        lib, wq, eta = self.genLib(len(self.parent.xax), I, maxSigma * wqMax * 1e6, numWq, numEta, angleStuff, self.parent.current.freq, self.parent.current.sw, weight, self.axAdd)
        out['I'] = [I]
        out['lib'] = [lib]
        out['wq'] = [wq]
        out['eta'] = [eta]
        out['freq'] = [self.parent.current.freq]
        return (out, [I, lib, wq, eta, self.parent.current.freq])

    def disp(self, params, num):
        out = params[num]
        for name in self.SINGLENAMES:
            inp = out[name][0]
            if isinstance(inp, tuple):
                inp = checkLinkTuple(inp)
                out[name][0] = inp[2]*params[inp[4]][inp[0]][inp[1]] + inp[3]
        numExp = len(out[self.MULTINAMES[0]])
        for i in range(numExp):
            for name in self.MULTINAMES:
                inp = out[name][i]
                if isinstance(inp, tuple):
                    inp = checkLinkTuple(inp)
                    out[name][i] = inp[2]*params[inp[4]][inp[0]][inp[1]] + inp[3]
                if not np.isfinite(out[name][i]):
                    self.rootwindow.mainProgram.dispMsg("One of the inputs is not valid")
                    return
        tmpx = self.parent.xax
        outCurveBase = out['bgrnd'][0] + tmpx * out['slope'][0]
        outCurve = outCurveBase.copy()
        outCurvePart = []
        x = []
        for i in range(len(out['pos'])):
            x.append(tmpx)
            y = out['amp'][i] * quad2CzjzektensorFunc(out['sigma'][i], out['d'][i], out['pos'][i], out['lor'][i], out['gauss'][i], out['wq'][0], out['eta'][0], out['lib'][0], self.parent.current.freq, self.parent.current.sw, self.axAdd, self.axMult)
            outCurvePart.append(outCurveBase + y)
            outCurve += y
        self.parent.fitDataList[tuple(self.parent.locList)] = [tmpx, outCurve, x, outCurvePart]
        self.parent.showFid()

#################################################################################


def quad2CzjzekmpFit(xax, data1D, guess, args, queue, minmethod):
    try:
        fitVal = scipy.optimize.minimize(lambda *param: np.sum((data1D-quad2CzjzekfitFunc(param, xax, args))**2), guess, method=minmethod)
    except:
       fitVal = None
    queue.put(fitVal)

def quad2CzjzekfitFunc(params, allX, args):
    params = params[0]
    specName = args[0]
    specSlices = args[1]
    allParam = []
    for length in specSlices:
        allParam.append(params[length])
    allStruc = args[3]
    allArgu = args[4]
    fullTestFunc = []
    for n in range(len(allX)):
        x=allX[n]
        testFunc = np.zeros(len(x))
        param = allParam[n]
        numExp = args[2][n]
        struc = args[3][n]
        argu = args[4][n]
        sw = args[5][n]
        axAdd = args[6][n]
        axMult = args[7][n]
        parameters = {'bgrnd':0.0, 'slope':0.0, 'pos':0.0, 'd':0.0, 'sigma':0.0, 'amp':0.0, 'lor':0.0, 'gauss':0.0}
        parameters['I'] = argu[-1][0]
        parameters['lib'] = argu[-1][1]
        parameters['wq'] = argu[-1][2]
        parameters['eta'] = argu[-1][3]
        parameters['freq'] = argu[-1][4]
        for name in ['bgrnd', 'slope']:
            if struc[name][0][0] == 1:
                parameters[name] = param[struc[name][0][1]]
            elif struc[name][0][0] == 0:
                parameters[name] = argu[struc[name][0][1]]
            else:
                altStruc = struc[name][0][1]
                if struc[altStruc[0]][altStruc[1]][0] == 1:
                    parameters[name] = altStruc[2] * allParam[altStruc[4]][struc[altStruc[0]][altStruc[1]][1]] + altStruc[3]
                elif struc[altStruc[0]][altStruc[1]][0] == 0:
                    parameters[name] = altStruc[2] * allArgu[altStruc[4]][struc[altStruc[0]][altStruc[1]][1]] + altStruc[3]
        for i in range(numExp):
            for name in ['pos', 'd', 'sigma', 'amp', 'lor', 'gauss']:
                if struc[name][i][0] == 1:
                    parameters[name] = param[struc[name][i][1]]
                elif struc[name][i][0] == 0:
                    parameters[name] = argu[struc[name][i][1]]
                else:
                    altStruc = struc[name][i][1]
                    strucTarget = allStruc[altStruc[4]]
                    if strucTarget[altStruc[0]][altStruc[1]][0] == 1:
                        parameters[name] = altStruc[2] * allParam[altStruc[4]][strucTarget[altStruc[0]][altStruc[1]][1]] + altStruc[3]
                    elif strucTarget[altStruc[0]][altStruc[1]][0] == 0:
                        parameters[name] = altStruc[2] * allArgu[altStruc[4]][strucTarget[altStruc[0]][altStruc[1]][1]] + altStruc[3]
            testFunc += parameters['amp'] * quad2CzjzektensorFunc(parameters['sigma'], parameters['d'], parameters['pos'], parameters['lor'], parameters['gauss'], parameters['wq'], parameters['eta'], parameters['lib'], parameters['freq'], sw, axAdd, axMult)
        testFunc += parameters['bgrnd'] + parameters['slope'] * x
        fullTestFunc = np.append(fullTestFunc, testFunc)
    return fullTestFunc
    
def quad2CzjzektensorFunc(sigma, d, pos, width, gauss, wq, eta, lib, freq, sw, axAdd, axMult = 1):
    sigma *= 1e6
    pos = (pos / axMult) - axAdd
    wq = wq
    eta = eta
    if sigma == 0.0: #protect against devide by zero
        czjzek = np.zeros_like(wq)
        czjzek[:,0] = 1
    else:
        czjzek = wq**(d - 1) * eta / (np.sqrt(2 * np.pi) * sigma**d) * (1 - eta**2 / 9.0) * np.exp(-wq**2 / (2.0 * sigma**2) * (1 + eta**2 / 3.0))
    czjzek = czjzek / np.sum(czjzek)
    fid = np.sum(lib * czjzek[..., None], axis=(0, 1))
    t = np.arange(len(fid)) / sw
    apod = np.exp(-np.pi * np.abs(width) * t) * np.exp(-((np.pi * np.abs(gauss) * t)**2) / (4 * np.log(2)))
    apod[-1:int(-(len(apod) / 2 + 1)):-1] = apod[:int(len(apod) / 2)]
    spectrum = scipy.ndimage.interpolation.shift(np.real(np.fft.fft(fid * apod)), len(fid) * pos / sw)
    spectrum = spectrum / sw * len(spectrum)
    return spectrum

#################################################################################

def czjzekStaticsetAngleStuff(cheng):
    phi, theta, weight = zcw_angles(cheng, symm=2)
    angleStuff = [-27 / 8.0 * np.cos(theta)**4 + 15 / 4.0 * np.cos(theta)**2 - 3 / 8.0,
                  (-9 / 4.0 * np.cos(theta)**4 + 2 * np.cos(theta)**2 + 1 / 4.0) * np.cos(2 * phi),
                  -1 / 2.0 * np.cos(theta)**2 + 1 / 3.0 + (-3 / 8.0 * np.cos(theta)**4 + 3 / 4.0 * np.cos(theta)**2 - 3 / 8.0) * np.cos(2 * phi)**2]
    return weight, angleStuff
        
def czjzekMASsetAngleStuff(cheng):
    phi, theta, weight = zcw_angles(cheng, symm=2)
    angleStuff = [21 / 16.0 * np.cos(theta)**4 - 9 / 8.0 * np.cos(theta)**2 + 5 / 16.0,
                  (-7 / 8.0 * np.cos(theta)**4 + np.cos(theta)**2 - 1 / 8.0) * np.cos(2 * phi),
                  1 / 12.0 * np.cos(theta)**2 + (+7 / 48.0 * np.cos(theta)**4 - 7 / 24.0 * np.cos(theta)**2 + 7 / 48.0) * np.cos(2 * phi)**2]
    return weight, angleStuff
