#!/usr/bin/python
#-*- coding: utf-8 -*-

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy, math, pdb, sys, random
import time, os, itertools, shutil, importlib
from tuneThreshold import tuneThresholdfromScore
from DatasetLoader import loadWAV

device = 'cuda' if torch.cuda.is_available() else 'cpu'

def chunks(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        if lst[i:i + n]:
          yield lst[i:i + n]

class SpeakerNet(nn.Module):

    def __init__(self, model, optimizer, scheduler, trainfunc, **kwargs):
        super(SpeakerNet, self).__init__();

        SpeakerNetModel = importlib.import_module('models.'+model).__getattribute__('MainModel')
        self.__S__ = SpeakerNetModel(**kwargs).to(device);

        LossFunction = importlib.import_module('loss.'+trainfunc).__getattribute__('LossFunction')
        self.__L__ = LossFunction(**kwargs).to(device);

        Optimizer = importlib.import_module('optimizer.'+optimizer).__getattribute__('Optimizer')
        self.__optimizer__ = Optimizer(self.parameters(), **kwargs)

        Scheduler = importlib.import_module('scheduler.'+scheduler).__getattribute__('Scheduler')
        self.__scheduler__, self.lr_step = Scheduler(self.__optimizer__, **kwargs)

        assert self.lr_step in ['epoch', 'iteration']

    ## ===== ===== ===== ===== ===== ===== ===== =====
    ## Train network
    ## ===== ===== ===== ===== ===== ===== ===== =====

    def train_network(self, loader):

        self.train();

        stepsize = loader.batch_size;

        counter = 0;
        index   = 0;
        loss    = 0;
        top1    = 0     # EER or accuracy

        tstart = time.time()

        for data, data_label in loader:

            data = data.transpose(0,1)

            self.zero_grad();

            feat = []
            for inp in data:
                outp      = self.__S__.forward(inp.to(device))
                feat.append(outp)

            feat = torch.stack(feat,dim=1).squeeze()

            label   = torch.LongTensor(data_label).to(device)

            nloss, prec1 = self.__L__.forward(feat,label)

            loss    += nloss.detach().cpu();
            top1    += prec1
            counter += 1;
            index   += stepsize;

            nloss.backward();
            self.__optimizer__.step();

            telapsed = time.time() - tstart
            tstart = time.time()

            sys.stdout.write("\rProcessing (%d) "%(index));
            sys.stdout.write("Loss %f TEER/TAcc %2.3f%% - %.2f Hz "%(loss/counter, top1/counter, stepsize/telapsed));
            sys.stdout.flush();

            if self.lr_step == 'iteration': self.__scheduler__.step()

        if self.lr_step == 'epoch': self.__scheduler__.step()

        sys.stdout.write("\n");

        return (loss/counter, top1/counter);


    ## ===== ===== ===== ===== ===== ===== ===== =====
    ## Evaluate from list
    ## ===== ===== ===== ===== ===== ===== ===== =====

    def evaluateFromList(self, listfilename, print_interval=100, test_path='', num_eval=0, eval_frames=None, step=0.2, save_path="./"):

        self.eval();

        lines       = []
        files       = []
        feats       = {}
        tstart      = time.time()

        ## Read all lines
        with open(listfilename) as listfile:
            while True:
                line = listfile.readline();
                if (not line):
                    break;

                data = line.strip();

                ## Append random label if missing
                # if len(data) == 2: data = [random.randint(0,1)] + data

                files.append(data)
                # files.append(data[2])
                lines.append(line)

        setfiles = list(set(files))
        setfiles.sort()

        ## Save all features to file
        for idx, file in enumerate(setfiles):
            wavs = loadWAV(os.path.join(test_path,file+'.wav'), eval_frames, evalmode=True, num_eval=num_eval, step=step)

            print('wavs size', len(wavs))
            res = []
            for c in chunks(wavs, 20):
              c = numpy.stack(c, axis=0).astype(numpy.float)
              inp1 = torch.FloatTensor(c).to(device)

              ref_feat = self.__S__.forward(inp1).detach().cpu()
              res.append(ref_feat)

            res = torch.cat(res)

            # import pickle
            # pickle.dump(res,  open( f"{file}.p", "wb" ) )

            with open(f'{save_path}/{file}.npy', 'wb') as f:
                numpy.save(f, res)

            filename = '%06d.wav'%idx

            feats[file]     = ref_feat

            telapsed = time.time() - tstart

            if idx % print_interval == 0:
                sys.stdout.write("\rReading %d of %d: %.2f Hz, embedding size %d"%(idx,len(setfiles),idx/telapsed,ref_feat.size()[1]));

        return
        all_scores = [];
        all_labels = [];
        all_trials = [];
        tstart = time.time()

        ## Read files and compute all scores
        for idx, line in enumerate(lines):

            data = line.split();

            ## Append random label if missing
            if len(data) == 2: data = [random.randint(0,1)] + data

            ref_feat = feats[data[1]].to(device)
            com_feat = feats[data[2]].to(device)

            if self.__L__.test_normalize:
                ref_feat = F.normalize(ref_feat, p=2, dim=1)
                com_feat = F.normalize(com_feat, p=2, dim=1)

            dist = F.pairwise_distance(ref_feat.unsqueeze(-1), com_feat.unsqueeze(-1).transpose(0,2)).detach().cpu().numpy();

            score = -1 * numpy.mean(dist);

            all_scores.append(score);
            all_labels.append(int(data[0]));
            all_trials.append(data[1]+" "+data[2])

            if idx % print_interval == 0:
                telapsed = time.time() - tstart
                sys.stdout.write("\rComputing %d of %d: %.2f Hz"%(idx,len(lines),idx/telapsed));
                sys.stdout.flush();

        print('\n')

        return (all_scores, all_labels, all_trials);


    ## ===== ===== ===== ===== ===== ===== ===== =====
    ## Save parameters
    ## ===== ===== ===== ===== ===== ===== ===== =====

    def saveParameters(self, path):

        torch.save(self.state_dict(), path);


    ## ===== ===== ===== ===== ===== ===== ===== =====
    ## Load parameters
    ## ===== ===== ===== ===== ===== ===== ===== =====

    def loadParameters(self, path):

        self_state = self.state_dict();
        loaded_state = torch.load(path, map_location=torch.device(device));
        for name, param in loaded_state.items():
            origname = name;
            if name not in self_state:
                name = name.replace("module.", "");

                if name not in self_state:
                    print("%s is not in the model."%origname);
                    continue;

            if self_state[name].size() != loaded_state[origname].size():
                print("Wrong parameter length: %s, model: %s, loaded: %s"%(origname, self_state[name].size(), loaded_state[origname].size()));
                continue;

            self_state[name].copy_(param);
