import datetime
import tensorflow as tf
import numpy as np
import matplotlib
matplotlib.use('Agg') ## for server
import matplotlib.pyplot as plt
import os.path
import time
import sys
import getopt
import pdb
import math
import logging
import scipy
import scipy.misc
import matplotlib.image as mpimg
from skimage.transform import resize
from sklearn.feature_extraction import image

from cnn_autoencoder.model import cnn_ae, cnn_ae_ethan
from cnn_autoencoder.cnn_ae_config import Config as conf

tf.set_random_seed(123)
np.random.seed(123)

def corrupt(data, nu, type='salt_and_pepper'):
    """
    Corrupts the data for inputing into the de-noising autoencoder

    Args:
        data: numpy array of size (num_points, 1, img_size, img_size)
        nu: corruption level
    Returns:
        numpy array of size (num_points, 1, img_size, img_size)
    """
    if type == 'salt_and_pepper':
        img_max = np.ones(data.shape, dtype=bool)
        tmp = np.copy(data)
        img_max[data <= 0.5] = False
        img_min = np.logical_not(img_max)
        idx = np.random.choice(a = [True, False], size=data.shape, p=[nu, 1-nu])
        tmp[np.logical_and(img_max, idx)] = 0
        tmp[np.logical_and(img_min, idx)] = 1
    return tmp

def extract_patches(filename_base, num_images, patch_size=conf.patch_size, phase='train'):
    patches = []
    for i in range(1, num_images+1):
        if phase == 'train':
            imageid = "satImage_%.3d" % i
            image_filename = filename_base + imageid + ".png"
            if os.path.isfile(image_filename):
                img = mpimg.imread(image_filename)
                img = resize(img, (50,50))
                patches.append(image.extract_patches(img, (patch_size, patch_size), extraction_step=1))
                patches.append(image.extract_patches(np.rot90(img), (patch_size, patch_size), extraction_step=1))
        if phase == 'test':
            imageid = "raw_test_%d_pixels" % i
            image_filename = filename_base + imageid + ".png"
            if os.path.isfile(image_filename):
                img = mpimg.imread(image_filename)
                img = resize(img, (38,38))
                patches.append(image.extract_patches(img, (patch_size, patch_size), extraction_step=1))
    return patches

def reconstruction(img_data, size):
    """
    Reconstruct single image from flattened array.
    IMPORTANT: overlapping patches are averaged, not replaced like in recontrustion()
    Args:
        img_data: flattened image array
        type: size of the image (rescaled)
    Returns:
        recontructed image
    """
    patches_per_dim = size - conf.patch_size + 1

    print("size: {}".format(size))
    print("patches_per_dim: {}".format(patches_per_dim))
    print("img_data: {}".format(img_data.shape))
    reconstruction = np.zeros((size,size))
    n = np.zeros((size,size))
    idx = 0
    for i in range(patches_per_dim):
        for j in range(patches_per_dim):
            reconstruction[i:(i+conf.patch_size),j:(j+conf.patch_size)] += img_data[idx,:].reshape(conf.patch_size, conf.patch_size)
            n[i:(i+conf.patch_size),j:(j+conf.patch_size)] += 1
            idx += 1
    return np.divide(reconstruction, n)

def resize_img(img, opt):
    """
    CNN predictions are made at the 36x36 pixel lvl and the test set needs to be at the 608x608
    lvl. The function resizes.
    Args:
        numpy array 36x36 for test or 50x50 for train
    Returns:
        numpy array 608x608 for test or 400x400 for train
    """
    print(img.shape)
    if opt == 'test':
        size = conf.test_image_size
        blocks = conf.cnn_res # resolution of cnn output of 16x16 pixels are the same class
        steps = conf.test_image_size // blocks # 38
    elif opt == 'train':
        size = conf.train_image_size
        blocks = conf.gt_res # resolution of the gt is 8x8 pixels for one class
        steps = conf.train_image_size // blocks # 50
    else:
        raise ValueError('test or train plz')
    dd = np.zeros((size, size))
    for i in range(steps):
        for j in range(steps):
            dd[j*blocks:(j+1)*blocks,i*blocks:(i+1)*blocks] = img[j,i]
    return dd

def mainFunc(argv):
    def printUsage():
        print('main.py -n <num_cores> -t <tag>')
        print('num_cores = Number of cores requested from the cluster. Set to -1 to leave unset')
        print('tag = optional tag or name to distinguish the runs, e.g. \'bidirect3layers\' ')

    num_cores = -1
    tag = None
    # Command line argument handling
    try:
        opts, args = getopt.getopt(argv,"n:t:",["num_cores=", "tag="])
    except getopt.GetoptError:
        printUsage()
        sys.exit(2)
    for opt, arg in opts:
        if opt == '-h':
            printUsage()
            sys.exit()
        elif opt in ("-n", "--num_cores"):
            num_cores = int(arg)
        elif opt in ("-t", "--tag"):
            tag = arg

    print("Executing autoencoder with {} CPU cores".format(num_cores))
    if num_cores != -1:
        # We set the op_parallelism_threads in the ConfigProto and pass it to the TensorFlow session
        configProto = tf.ConfigProto(inter_op_parallelism_threads=num_cores,
                                     intra_op_parallelism_threads=num_cores)
    else:
        configProto = tf.ConfigProto()

    print("loading ground truth data")
    train_data_filename = "../data/training/groundtruth/"
    targets = extract_patches(train_data_filename, conf.train_size, conf.patch_size, 'train')
    targets = np.stack(targets).reshape(-1, conf.patch_size, conf.patch_size) # (20000, 16, 16)
    targets = targets.reshape(len(targets), -1) # (122500, 256) for no rot (145800, 576) for rot and patch size 24
    train_full = np.copy(targets)
    print("Shape of targets: {}".format(targets.shape))
    patches_per_image_train = ( (conf.train_image_size//conf.gt_res) - conf.patch_size + 1)**2 ## conf.train_image_size//conf.gt_res = 50 res of gt is 8x8
    print("Patches per train image: {}".format(patches_per_image_train)) # 729 for patch size 24
    validation = np.copy(targets[:conf.val_size*patches_per_image_train,:]) # number of validation patches is 500
    targets = np.copy(targets[patches_per_image_train*conf.val_size:,:])

    print("Adding noise to training data")
    train = corrupt(targets, conf.corruption)
    validation = corrupt(validation, conf.corruption)

    print("Initializing CNN denoising autoencoder")
    # model = cnn_ae(conf.patch_size**2, ## dim of the inputs
    #                n_filters=[1, 16, 32, 64],
    #                filter_sizes=[7, 5, 3, 3],
    #                learning_rate=conf.learning_rate)
    model = cnn_ae_ethan(conf.patch_size, ## dim of the inputs Not patch_size**2
                         learning_rate=conf.learning_rate)

    print("Starting TensorFlow session")
    with tf.Session(config=configProto) as sess:
        start = time.time()
        global_step = 1

        saver = tf.train.Saver(max_to_keep=3, keep_checkpoint_every_n_hours=2)

        # Init Tensorboard summaries. This will save Tensorboard information into a different folder at each run.
        timestamp = '{0:%Y-%m-%d_%H-%M-%S}'.format(datetime.datetime.now())
        tag_string = ""
        if tag is not None:
            tag_string = tag
        train_logfolderPath = os.path.join(conf.log_directory, "cnn-ae-{}-training-{}".format(tag_string, timestamp))
        train_writer        = tf.summary.FileWriter(train_logfolderPath, graph=tf.get_default_graph())

        sess.run(tf.global_variables_initializer())

        sess.graph.finalize()

        print("Starting training")
        for i in range(conf.num_epochs):
            print("Training epoch {}".format(i))
            print("Time elapsed:    %.3fs" % (time.time() - start))

            n = train.shape[0]
            perm_idx = np.random.permutation(n)
            batch_index = 1
            for step in range(int(n / conf.batch_size)):
                offset = (batch_index*conf.batch_size) % (n - conf.batch_size)
                batch_indices = perm_idx[offset:(offset + conf.batch_size)]

                batch_inputs = train[batch_indices,:]
                batch_targets = targets[batch_indices,:]
                feed_dict = model.make_inputs(batch_inputs, batch_targets)

                _, train_summary = sess.run([model.optimizer, model.summary_op], feed_dict)
                train_writer.add_summary(train_summary, global_step)

                global_step += 1
                batch_index += 1

        saver.save(sess, os.path.join(train_logfolderPath, "cnn-ae-{}-{}-ep{}-final.ckpt".format(tag_string, timestamp, conf.num_epochs)))

        # Deleting train and targets objects
        del train
        del targets

        if conf.run_on_train_set:
            print("Running Convolutional Autoencoder on training images for upstream classification")
            predictions = []
            runs = train_full.shape[0] // conf.batch_size
            rem = train_full.shape[0] % conf.batch_size
            for i in range(runs):
                batch_inputs = train_full[i*conf.batch_size:((i+1)*conf.batch_size),:]
                feed_dict = model.make_inputs_predict(batch_inputs)
                prediction = sess.run(model.y_pred, feed_dict) ## numpy array (50, 76, 76, 1)
                predictions.append(prediction)
            if rem > 0:
                batch_inputs = train_full[runs*conf.batch_size:(runs*conf.batch_size + rem),:]
                feed_dict = model.make_inputs_predict(batch_inputs)
                prediction = sess.run(model.y_pred, feed_dict)
                predictions.append(prediction)

            print("individual prediction shape: {}".format(predictions[0].shape))
            predictions = np.concatenate(predictions, axis=0).reshape(train_full.shape[0], conf.patch_size**2)
            #predictions = predictions.reshape(len(predictions), -1)
            print("Shape of predictions: {}".format(predictions.shape)) # (116375, 256)

            # Save outputs to disk
            for i in range(conf.train_size):
                print("Train img: " + str(i+1))
                img_name = "cnn_ae_train_" + str(i+1)
                output_path = "../results/CNN_Autoencoder_Output/train/"
                if not os.path.isdir(output_path):
                    raise ValueError('no CNN data to run Convolutional Denoising Autoencoder on')
                prediction = reconstruction(predictions[i*patches_per_image_train:(i+1)*patches_per_image_train,:], 50)
                # resizing test images to 400x400 and saving to disk
                scipy.misc.imsave(output_path + img_name + ".png", resize_img(prediction, 'train'))

        if conf.visualise_validation:
            print("Visualising encoder results and true images from train set")
            f, a = plt.subplots(2, conf.examples_to_show, figsize=(conf.examples_to_show, 5))
            for i in range(conf.examples_to_show):
                inputs = validation[i*patches_per_image_train:(i+1)*patches_per_image_train,:]
                feed_dict = model.make_inputs_predict(inputs)
                encode_decode = sess.run(model.y_pred, feed_dict=feed_dict) ## predictions from model are [batch_size, dim, dim, n_channels] i.e. (3125, 16, 16, 1)
                print("shape of predictions: {}".format(encode_decode.shape)) # (100, 16, 16, 1)
                val = reconstruction(inputs, 50)
                pred = reconstruction(encode_decode[:,:,:,0].reshape(-1, conf.patch_size**2), 50) ## train images rescaled to 50 by 50 granularity
                a[0][i].imshow(val, cmap='gray', interpolation='none')
                a[1][i].imshow(pred, cmap='gray', interpolation='none')
                a[0][i].get_xaxis().set_visible(False)
                a[0][i].get_yaxis().set_visible(False)
                a[1][i].get_xaxis().set_visible(False)
                a[1][i].get_yaxis().set_visible(False)
            plt.gray()
            plt.savefig('./cnn_autoencoder_eval_{}.png'.format(tag))

        if conf.run_on_test_set:
            print("Running the Convolutional Denoising Autoencoder on the predictions")
            prediction_test_dir = "../results/CNN_Output/test/high_res_raw/"
            if not os.path.isdir(prediction_test_dir):
                raise ValueError('no CNN data to run Convolutional Denoising Autoencoder on')

            print("Loading test set")
            patches_per_image_test = ( (conf.test_image_size // conf.cnn_res) - conf.patch_size + 1)**2 ## 608 / 16 = 38, where 16 is the resolution of the CNN output
            print("patches per test image: {}".format(patches_per_image_test))
            test = extract_patches(prediction_test_dir, conf.test_size, conf.patch_size, 'test')
            test = np.stack(test).reshape(-1, conf.patch_size, conf.patch_size) # (n, 16, 16)
            test = test.reshape(len(test), -1) # (n, 256)
            print("Shape of test: {}".format(test.shape)) # Shape of test: (26450, 256)

            predictions = []
            runs = test.shape[0] // conf.batch_size
            rem = test.shape[0] % conf.batch_size
            for i in range(runs):
                batch_inputs = test[i*conf.batch_size:((i+1)*conf.batch_size),:]
                feed_dict = model.make_inputs_predict(batch_inputs)
                prediction = sess.run(model.y_pred, feed_dict) ## numpy array (50, 76, 76, 1)
                predictions.append(prediction)
            if rem > 0:
                batch_inputs = test[runs*conf.batch_size:(runs*conf.batch_size + rem),:]
                feed_dict = model.make_inputs_predict(batch_inputs)
                prediction = sess.run(model.y_pred, feed_dict)
                predictions.append(prediction)

            print("individual prediction shape: {}".format(predictions[0].shape))
            predictions = np.concatenate(predictions, axis=0).reshape(test.shape[0], conf.patch_size**2)
            #predictions = predictions.reshape(len(predictions), -1)
            print("Shape of predictions: {}".format(predictions.shape))

            # Save outputs to disk
            for i in range(conf.test_size):
                print("Test img: " + str(i+1))
                img_name = "cnn_ae_test_" + str(i+1)
                output_path = "../results/CNN_Autoencoder_Output/test/"
                if not os.path.isdir(output_path):
                    raise ValueError('no CNN data to run Convolutional Denoising Autoencoder on')
                prediction = reconstruction(predictions[i*patches_per_image_test:(i+1)*patches_per_image_test,:], 38) # 38 is the resized test set dim as resolution is 16x16
                # resizing test images to 608x608 and saving to disk
                scipy.misc.imsave(output_path + img_name + ".png", resize_img(prediction, 'test'))

            f, a = plt.subplots(2, conf.examples_to_show, figsize=(conf.examples_to_show, 5))
            for i in range(conf.examples_to_show):
                t = reconstruction(test[i*patches_per_image_test:(i+1)*patches_per_image_test,:], (conf.test_image_size // conf.cnn_res)) # (conf.test_image_size // conf.cnn_res) = 38
                pred = reconstruction(predictions[i*patches_per_image_test:(i+1)*patches_per_image_test,:], 38)
                a[0][i].imshow(t, cmap='gray', interpolation='none')
                a[1][i].imshow(pred, cmap='gray', interpolation='none')
                a[0][i].get_xaxis().set_visible(False)
                a[0][i].get_yaxis().set_visible(False)
                a[1][i].get_xaxis().set_visible(False)
                a[1][i].get_yaxis().set_visible(False)
            plt.gray()
            plt.savefig('./cnn_autoencoder_prediction_{}.png'.format(tag))

            print("Finished saving cnn autoencoder test set to disk")

if __name__ == "__main__":
    #logging.basicConfig(filename='autoencoder.log', level=logging.DEBUG)
    mainFunc(sys.argv[1:])