import argparse
import os
from ast import arg
from datetime import datetime
import random 
import numpy as np
import torch
import torch.nn as nn
from src.decoders.greedy_decoder import GreedyDecoder
from src.metrics.MetricCalculator import MetricCalculator
from src.models import Encoder_AttnRNN, Encoder_RNN, Encoder_Transformer
from src.constants import CHECKPOINTS_DIR, LXMERT_HIDDEN_SIZE
from src.data.datasets import GenVQADataset, pad_batched_sequence
from src.logger import Instance as Logger
from torch.utils.data.dataloader import DataLoader
from torchmetrics import Accuracy, F1Score
from tqdm import tqdm
import json
class VQA:
    def __init__(self,
                 train_date,
                 model,
                 train_dset,
                 val_dset=None,
                 tokenizer=None,
                 use_cuda=True,
                 batch_size=32,
                 epochs=200,
                 lr=0.005,
                 log_every=1,
                 save_every=50):
        
        self.model = model
        self.epochs = epochs
        self.batch_size = batch_size
        self.log_every = log_every
        self.train_date_time = train_date
        self.save_every = save_every
        
        self.train_loader = DataLoader(train_dset, batch_size=batch_size, shuffle=True, drop_last=True, collate_fn=pad_batched_sequence)
        self.val_loader = DataLoader(val_dset, batch_size=batch_size, shuffle=False, drop_last=True, collate_fn=pad_batched_sequence)
        
        if(use_cuda):
            self.model = self.model.cuda()
            
        pad_idx = 0
        self.criterion = nn.CrossEntropyLoss(ignore_index=pad_idx)
        self.optim = torch.optim.Adam(list(self.model.parameters()), lr=lr)
        
        self.f1_score = F1Score(num_classes=self.model.Tokenizer.vocab_size, ignore_index=pad_idx, top_k=1, mdmc_average='samplewise')
        self.accuracy = Accuracy(num_classes=self.model.Tokenizer.vocab_size, ignore_index=pad_idx, top_k=1, mdmc_average='samplewise')
        
        self.save_dir = os.path.join(CHECKPOINTS_DIR, str(self.train_date_time))
        
    def train(self):
        running_loss = running_accuracy = running_accuracy_best = running_f1 = 0
        results = {}
        for epoch in range(self.epochs):
            for i, (input_ids, feats, boxes, masks, target, target_masks) in enumerate(pbar := tqdm(self.train_loader, total=len(self.train_loader))):

                self.model.train()
                
                pbar.set_description(f"Epoch {epoch}")
                loss, batch_acc, batch_f1, _, _ = self.__step(input_ids, feats, boxes, masks, target, target_masks, val=False)  
                
                running_loss += loss.item()
                running_accuracy += batch_acc.item()
                running_f1 += batch_f1
                
                pbar.set_postfix(loss=running_loss/(i+1), accuracy=running_accuracy/(i+1))
            
            if epoch % self.log_every == self.log_every - 1:
                val_loss = None
                val_acc = None
                pred_sentences = []
                ref_sentences = []
                if(self.val_loader):
                    self.model.eval()

                    val_loss = val_acc = val_f1 = 0
                    
                    for i, (input_ids, feats, boxes, masks, target, target_masks) in enumerate(self.val_loader):
                        val_loss, val_acc_batch, val_f1_batch, preds, refs = self.__step(input_ids, feats, boxes, masks, target, target_masks, val=True)
                        val_loss += loss.item()
                        val_acc += val_acc_batch
                        val_f1 += val_f1_batch
                        pred_sentences.extend(preds)
                        ref_sentences.extend(refs)
                        
                    val_loss /= len(self.val_loader)
                    val_acc /= len(self.val_loader)
                    val_f1 /= len(self.val_loader)
                    print("Calculating qualification metrics")
                    metric_calculator = MetricCalculator(self.model.Tokenizer, self.model.embedding_layer.cpu())
                    results[epoch] = {
                                        "qualification_metics": metric_calculator.compute(pred_sentences, ref_sentences), 
                                        "loss": val_loss,
                                        "accuracy": val_acc,
                                        "f1" : val_f1
                    }

                total_data_iterated = self.log_every * len(self.train_loader)
                running_loss /= total_data_iterated
                running_accuracy /= total_data_iterated
                running_f1 /= total_data_iterated
               


                if(self.val_loader):
                    Logger.log(f"Train_{self.train_date_time}", f"Training epoch {epoch}: Train loss {running_loss:.3f}. Val loss: {val_loss:.3f}."
                                + f" Train accuracy {running_accuracy:.3f}. Val accuracy: {val_acc:.3f}. Train F1-Score: {running_f1}. Validation F1-Score: {val_f1}")
                    print(f"F1 Score: Train {running_f1}, Validation: {val_f1}")
                else:
                    Logger.log(f"Train_{self.train_date_time}", f"Training epoch {epoch}: Train loss {running_loss:.3f}."
                                + f" Train accuracy {running_accuracy:.3f}. Train F1-Score: {running_f1}")
                    print(f"F1 Score: Train {running_f1}")

                
                if(running_accuracy > running_accuracy_best):
                    self.model.save(self.save_dir, f"BEST")
                    running_accuracy_best = running_accuracy
                
                running_loss = running_accuracy = running_f1 = 0

            if(epoch % self.save_every == self.save_every - 1):
                self.model.save(self.save_dir, epoch)
        with open(os.path.join(self.save_dir, "validation_results.json")) as fp: 
            json.dump(results, fp)
    
    def __step(self, input_ids, feats, boxes, masks, target, target_masks, val=False): 
        teacher_force_ratio = 0 if val else 0.5       
        logits = self.model(input_ids, feats, boxes, masks, target, teacher_force_ratio)
        # logits shape: (L, N, target_vocab_size)
        loss = self.criterion(logits.permute(1, 2, 0), target.permute(1,0))
        #validation only!
        pred_sentences = None
        ref_sentences = None
        if not(val):
            self.optim.zero_grad()
            loss.backward()
            self.optim.step()
        else:
            # get ready for qualifacation metrics (BLEU, ROUGE, etc)
            decoder = GreedyDecoder(self.model.Tokenizer)
            pred_sentences = decoder.decode_from_logits(logits)
            ref_sentences = decoder.batch_decode(target)

        f1_score = self.f1_score(logits.permute(1,2,0), target.permute(1,0))
        batch_acc = self.accuracy(logits.permute(1,2,0), target.permute(1,0))

        assert batch_acc <= 1
        return loss, batch_acc, f1_score, ref_sentences, pred_sentences
                
def parse_args():
    parser = argparse.ArgumentParser()
    #specify seed for reproducing
    parser.add_argument("--seed", default=8956, type=int)

    #specify encoder type, options: lxmert, visualbert 
    parser.add_argument("--encoder_type", default="rnn", type=str)
    
    #specify decoder type, options: rnn, attn-rnn 
    parser.add_argument("--decoder_type", default="rnn", type=str)
    
    #RNN specifications
    parser.add_argument("--rnn_type", default="lstm", type=str) #options: lstm, gru
    parser.add_argument("--num_rnn_layers", default=1, type=int)
    parser.add_argument("--bidirectional", default=False, action="store_true")
    
    # Attention RNN specifications
    parser.add_argument("--attn_type", default="bahdanau", type=str) #options: bahdanau, luong
    # use only when attention type is luong
    parser.add_argument("--attn_method", default="dot", type=str) #options: dot, general, concat
    
    #Transformer specifications
    parser.add_argument("--nheads", default=12, type=int)
    parser.add_argument("--num_transformer_layers", default=6, type=int)

    return parser.parse_args()




if __name__ == "__main__":
    args = parse_args()

    torch.manual_seed(args.seed)
    random.seed(args.seed)
    np.random.seed(args.seed)

    model = None
    if (args.decoder_type.lower() == 'rnn'):
        model = Encoder_RNN.Encoder_RNN(encoder_type=args.encoder_type,
                                        rnn_type=args.rnn_type, 
                                        num_layers=args.num_rnn_layers, 
                                        bidirectional=args.bidirectional).cuda()
    
    elif (args.decoder_type.lower() == 'transformer'):
        model = Encoder_Transformer.Encoder_Transformer(encoder_type=args.encoder_type,
                                                        nheads=args.nheads,
                                                        decoder_layers=args.num_transformer_layers,
                                                        hidden_size=LXMERT_HIDDEN_SIZE).cuda()
        
    elif(args.decoder_type.lower() == 'attn-rnn'):
        model = Encoder_AttnRNN.Encoder_AttnRNN(encoder_type = args.encoder_type,
                                                rnn_type=args.rnn_type,
                                                attn_type = args.attn_type,
                                                attn_method=args.attn_method).cuda()
                                   
    train_dset = GenVQADataset(model.Tokenizer, 
        annotations = "../fsvqa_data_train_full/annotations.pickle", 
        questions = "../fsvqa_data_train_full/questions.pickle", 
        img_dir = "../img_data")
    val_dset = GenVQADataset(model.Tokenizer, 
        annotations = "../fsvqa_data_val_full/annotations.pickle", 
        questions = "../fsvqa_data_val_full/questions.pickle", 
        img_dir = "../val_img_data")
    
    if model:
        vqa = VQA(datetime.now() , model, train_dset, val_dset=val_dset)
        vqa.train()
