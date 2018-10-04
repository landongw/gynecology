import os
import keras
import random
import argparse
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


from keras.models import Model, load_model
from keras.callbacks import ModelCheckpoint, ReduceLROnPlateau, EarlyStopping
from keras.optimizers import Adam, SGD, Adamax

from sklearn.metrics import confusion_matrix, classification_report, accuracy_score
from sklearn.model_selection import train_test_split
from sklearn.utils.class_weight import compute_class_weight

import utils as myutils
from model import build_model

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-d' ,'--data_dir', type=str, default='/data/put_data/cmchang/gynecology/data/', help='data directory')
    parser.add_argument('-s' ,'--model_save', type=str, default='', help='model save path')
    parser.add_argument('-y' ,'--target', type=str, default=None, help='prediction target')

    # input parameter
    parser.add_argument('-l' ,'--length', type=int, default=300, help='length of input')
    parser.add_argument('-c' ,'--n_channel', type=int, default=2, help='number of input channels')
    parser.add_argument('-rn','--random_noise', type=int, default=0, help='add Gaussian noise (mean=0, std=0.01) into inputs')
    parser.add_argument('-nm','--normalized', type=int, default=1, help='whether conduct channel-wise normalization')
    parser.add_argument('-ks','--k_slice', type=int, default=5, help='a input will be sliced into k_slice segments when testing')

    # model parameters
    parser.add_argument('-k' ,'--kernel_size', type=int, default=3, help='kernel size')
    parser.add_argument('-f' ,'--filters', type=int, default=64, help='base number of filters')
    parser.add_argument('-ly' ,'--layers', type=int, default=10, help='number of residual layers')
    parser.add_argument('-a' ,'--activation', type=str, default='relu', help='activation function')
    parser.add_argument('-i' ,'--kernel_initializer', type=str, default='RandomNormal', help='kernel initialization method')
    parser.add_argument('-l2','--l2', type=float, default=0.0, help='coefficient of l2 regularization')

    # hyper-parameters
    parser.add_argument('-bs','--batch_size', type=int, default=16, help='batch_size')
    parser.add_argument('-ep','--epoch', type=int, default=100, help='epoch')
    parser.add_argument('-wb','--weight_balance', type=int, default=1, help='whether weight balancing or not')
    parser.add_argument('-th','--acceptable_zeros_threshold', type=float, default=90, help='acceptable number of missing values in raw data')
    parser.add_argument('-g' ,'--gpu_id', type=str, default='0', help='GPU ID')
    parser.add_argument('-rs' ,'--random_state', type=int, default=13, help='random state when train_test_split')
    parser.add_argument('-fn' ,'--summary_file', type=str, default=None, help='summary filename')

    FLAG = parser.parse_args()

    print("===== create directory =====")
    if not os.path.exists(FLAG.model_save):
        os.makedirs(FLAG.model_save)
    
    print("===== train =====")

    train(FLAG)

def train(FLAG):
    os.environ['CUDA_VISIBLE_DEVICES'] = FLAG.gpu_id

    d = pd.read_csv(os.path.join(FLAG.data_dir, 'data_merged.csv'))
    d = d[myutils.get_n_zeros(np.array(d[[k for k in d.columns if 'b-' in k]], dtype=np.float)) <= FLAG.acceptable_zeros_threshold]

    n_classes = len(set(d[FLAG.target]))

    # replace 0 (no readings) with np.nan for later substitution
    for k in d.columns:
        if 'b-' in k or 'm-' in k:
            print(k, end='\r')
            d.loc[d[k]==0, k] = np.nan

    # train test split
    
    train_id, valid_id = train_test_split(list(set(d.ID)), test_size=0.3, random_state=FLAG.random_state)
    train_d, valid_d = d[[k in set(train_id) for k in d.ID]], d[[k in set(valid_id) for k in d.ID]]

    # interpolate missing values
    train_db = np.array(train_d[[k for k in train_d.columns if 'b-' in k]].interpolate(limit_direction='both', axis=1), dtype=np.float)
    train_dm = np.array(train_d[[k for k in train_d.columns if 'm-' in k]].interpolate(limit_direction='both', axis=1), dtype=np.float)

    valid_db = np.array(valid_d[[k for k in valid_d.columns if 'b-' in k]].interpolate(limit_direction='both', axis=1), dtype=np.float)
    valid_dm = np.array(valid_d[[k for k in valid_d.columns if 'm-' in k]].interpolate(limit_direction='both', axis=1), dtype=np.float)

    # combine signals from baby and mom
    Xtrain = np.stack([train_db, train_dm], axis=2)
    Xvalid = np.stack([valid_db, valid_dm], axis=2)

    # convert labels to one-hot encodings
    Ytrain = keras.utils.to_categorical(np.array(train_d[FLAG.target]), num_classes=n_classes)
    Yvalid = keras.utils.to_categorical(np.array(valid_d[FLAG.target]), num_classes=n_classes)

    # weight balancing or not
    if FLAG.weight_balance:
        
        y_integers = np.argmax(Ytrain, axis=1)
        d_class_weight = compute_class_weight('balanced', np.unique(y_integers), y_integers)
        class_weight = dict(enumerate(d_class_weight))
        print('class weight: {0}'.format(class_weight))
    else:
        class_weight = dict()
        for i in range(n_classes):
            class_weight[i] = 1

    # k fold of validation set
    Xtest, Ytest, Wtest = myutils.k_slice_X(Xvalid, Yvalid, length=FLAG.length, k_slice=FLAG.k_slice, class_weight = class_weight)

    if not os.path.exists(FLAG.model_save):
        os.mkdir(FLAG.model_save)
        print('directory {0} is created.'.format(FLAG.model_save))
    else:
        print('directory {0} already exists.'.format(FLAG.model_save))

    def my_generator(Xtrain, Ytrain, length, n_channel, n_classes, random_noise, normalized, batch_size):
        n_sample = Xtrain.shape[0]
        n_length = Xtrain.shape[1]
        ind = list(range(n_sample))
        x = np.empty((batch_size, length, n_channel), dtype=np.float)
        y = np.empty((batch_size, n_classes), dtype=int)

        while True:
            np.random.shuffle(ind)
            for i in range(n_sample//batch_size):
                st = random.choice(np.arange(0, Xtrain.shape[1] - length))
                i_batch = ind[i*batch_size:(i+1)*batch_size]
                for j, k in enumerate(i_batch):
                    x[j,:] = myutils.data_preprocess(Xtrain[k,st:(st+length),:], random_noise=random_noise, normalized=normalized)
                    y[j,:] = Ytrain[k,:]
                yield x, y

    # declare model 
    model = build_model(length=FLAG.length, n_channel=FLAG.n_channel, n_classes=n_classes, filters=FLAG.filters, kernel_size=FLAG.kernel_size, layers=FLAG.layers,
                    activation=FLAG.activation, kernel_initializer=FLAG.kernel_initializer, l_2=FLAG.l2)
    model.summary()

    lr_rate = 1e-5
    adam = Adamax(lr=lr_rate, beta_1=0.5, beta_2=0.999, epsilon=1e-08, decay = 0.0)
    model.compile(loss='categorical_crossentropy', optimizer=adam, metrics=['accuracy'])

    csv_logger = keras.callbacks.CSVLogger(os.path.join(FLAG.model_save, 'training.log'))
    checkpoint = keras.callbacks.ModelCheckpoint(os.path.join(FLAG.model_save, 'model.h5'), 
                                                monitor='val_loss', 
                                                verbose=1, 
                                                save_best_only=True,
                                                save_weights_only=False,
                                                mode='min',
                                                period=1)
    earlystop = EarlyStopping(monitor = 'val_loss', patience=20, verbose=1)
    reduce_lr = ReduceLROnPlateau(monitor='val_loss', factor = 0.5, patience = 10, min_lr = 0, cooldown = 5, verbose = True)

    # fit
    model.fit_generator(generator=my_generator(Xtrain, Ytrain, 
                                            length=FLAG.length, 
                                            n_channel=FLAG.n_channel, 
                                            n_classes=n_classes,
                                            random_noise=FLAG.random_noise,
                                            normalized=FLAG.normalized,
                                            batch_size=FLAG.batch_size),
                        class_weight=class_weight,
                        validation_data=(Xtest, Ytest, Wtest),
                        steps_per_epoch=50, 
                        epochs=FLAG.epoch,
                        verbose=0,
                        callbacks=[csv_logger,
                                reduce_lr, 
                                checkpoint,
                                earlystop])
    # plot csv logger
    myutils.plot_keras_csv_logger(csv_logger, save_dir=FLAG.model_save, accuracy=True)

    # evaluate validation set
    trained_model = load_model(os.path.join(FLAG.model_save,'model.h5'))
    Pred = trained_model.predict(Xtest)

    # evaluate by every segment
    ypred_aug = np.argmax(Pred , axis=1)
    ytest_aug = np.argmax(Ytest, axis=1)

    cfm = confusion_matrix(y_pred=ypred_aug, y_true=ytest_aug)

    plt.figure()
    myutils.plot_confusion_matrix(cfm, classes=np.arange(n_classes), title='Confusion matrix, without normalization')
    plt.savefig(os.path.join(FLAG.model_save, 'segment_confusion_matrix.png'))
    plt.close()

    # aggregate by voting
    ypred = (np.mean(ypred_aug.reshape(FLAG.k_slice,-1), axis=0) > 0.5) + 0 # voting
    ytest = np.argmax(Yvalid, axis=1)

    # calculate aggregated results
    cfm = confusion_matrix(y_pred=ypred, y_true=ytest)
    recall = np.diag(cfm) / np.sum(cfm, axis=1)
    precision = np.diag(cfm) / np.sum(cfm, axis=0)
    vote_val_accu = accuracy_score(y_pred=ypred, y_true=ytest)

    plt.figure()
    myutils.plot_confusion_matrix(cfm, classes=np.arange(n_classes), title='Confusion matrix, without normalization')
    plt.savefig(os.path.join(FLAG.model_save, 'voting_confusion_matrix.png'))
    plt.close()

    # read traing.log
    loss = pd.read_table(csv_logger.filename, delimiter=',')
    best_val_loss = np.min(loss.val_loss)
    best_epoch = np.argmin(loss.val_loss)

    # calculate average accuracy from segments
    # and voting accuracy
    tmp = ypred_aug.reshape(FLAG.k_slice,-1)
    savg_val_accu = 0.0
    for i in range(tmp.shape[0]):
        accu = accuracy_score(y_pred=tmp[i,:], y_true=ytest)
        print('{0}-segment accuracy={1}'.format(i, accu))
        savg_val_accu += accu
    savg_val_accu /= tmp.shape[0]
    print('avg accu={0}'.format(savg_val_accu))
    print('vote accu={0}'.format(vote_val_accu))

    # save into dictionary
    sav = vars(FLAG)
    sav['epoch'] = best_epoch
    sav['val_loss'] = best_val_loss
    sav['vote_val_accu'] = vote_val_accu
    sav['savg_val_accu'] = savg_val_accu
    
    for i in range(n_classes):
        sav['recall-{0}'.format(i)] = recall[i]
        sav['precision-{0}'.format(i)] = precision[i]

    # append into summary files
    dnew = pd.DataFrame(sav, index=[0])
    if os.path.exists(FLAG.summary_file):
        dori = pd.read_csv(FLAG.summary_file)
        dori = pd.concat([dori, dnew])
        dori.to_csv(FLAG.summary_file, index=False)
    else:
        dnew.to_csv(FLAG.summary_file, index=False)

    print(FLAG.summary_file)

if __name__ == '__main__':
    main()
