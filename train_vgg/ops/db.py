#!/usr/bin/env python
import os
import re
import sshtunnel
import itertools
import psycopg2
import psycopg2.extras
import psycopg2.extensions
import numpy as np
from scipy.stats import spearmanr
from tqdm import tqdm
from credentials import postgresql_connection, g15_credentials
from ops.preprocessing_tfrecords import create_heatmap
from ops.plotting_fun import save_mosaic, plot_image_hm
from scipy import misc
from copy import deepcopy


sshtunnel.DAEMON = True  # Prevent hanging process due to forward thread


def flatten(x):
    return list(itertools.chain(*x))


class db(object):
    def __init__(self, config):
        # Pass config -> this class
        for k, v in config.items():
            setattr(self, k, v)

    def __enter__(self):
        forward = sshtunnel.SSHTunnelForwarder(
            'g15.clps.brown.edu',
            ssh_username=self.username,
            ssh_password=self.password,
            remote_bind_address=('127.0.0.1', 5432))
        forward.start()
        pgsql_port = forward.local_bind_port
        pgsql_string = postgresql_connection(str(pgsql_port))
        self.forward = forward
        self.pgsql_port = pgsql_port
        self.pgsql_string = pgsql_string
        self.conn = psycopg2.connect(**pgsql_string)
        self.conn.set_isolation_level(
            psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        self.cur = self.conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.forward.close()
        if exc_type is not None:
            # print exc_type, exc_value, traceback
            raise
        return self

    def get_current_generation(self):
        self.cur.execute("select current_generation from image_count")
        return self.cur.fetchone()

    def get_click_coors(self, generations=None, set_names=['mirc', 'ilsvrc20'], exclude=None):
        print 'Getting clicks from DB'
        if generations is not None:
            self.cur.execute(
                "select * from click_paths where generation=(%s) and result in ('wrong','correct')", (generations))
        elif exclude is not None:
            self.cur.execute("select * from click_paths where result in ('wrong','correct') and user_id not in %s", (tuple(exclude),))
        else:
            self.cur.execute(
                "select * from click_paths where result in ('wrong','correct')"
                )
        return self.cur.fetchall()

    def consolidate_with_ids(self, clicks):
        print 'Consolidating clicks'
        all_image_ids = np.asarray(
            [x['image_id'] for x in clicks])  # Get all images
        unique_image_ids = np.unique(all_image_ids)  # Get unique images
        consolidated_clicks = []
        num_clicks = []
        # Consolidate clicks and info for each unique image
        for idx in tqdm(
                unique_image_ids, total=len(unique_image_ids)):
            it_clicks = clicks[all_image_ids == idx]
            if len(it_clicks) > 1:
                keep_clicks = it_clicks[0]
                # Append all clicks for this image into the same list
                for il in range(1, len(it_clicks)):
                    keep_clicks['clicks']['x'] = keep_clicks['clicks']['x'] +\
                        it_clicks[il]['clicks']['x']
                    keep_clicks['clicks']['y'] = keep_clicks['clicks']['y'] +\
                        it_clicks[il]['clicks']['y']
            consolidated_clicks.append(it_clicks[0])
            num_clicks.append(len(it_clicks))
        print 'Maximum number of clicks in a heatmap: %s' % np.max(num_clicks)
        return consolidated_clicks, num_clicks, unique_image_ids

    def consolidate_with_image_names(self, clicks):
        print 'Consolidating clicks'
        all_image_ids = np.asarray(
            [x['image_id'] for x in clicks])  # Get all images
        unique_image_ids = np.unique(all_image_ids)  # Get unique images
        # JOIN WITH IMAGE_NAMES HERE
        import ipdb;ipdb.set_trace()


        consolidated_clicks = []
        num_clicks = []
        # Consolidate clicks and info for each unique image
        for idx in tqdm(
                unique_image_ids, total=len(unique_image_ids)):
            it_clicks = clicks[all_image_ids == idx]
            if len(it_clicks) > 1:
                keep_clicks = it_clicks[0]
                # Append all clicks for this image into the same list
                for il in range(1, len(it_clicks)):
                    keep_clicks['clicks']['x'] = keep_clicks['clicks']['x'] +\
                        it_clicks[il]['clicks']['x']
                    keep_clicks['clicks']['y'] = keep_clicks['clicks']['y'] +\
                        it_clicks[il]['clicks']['y']
            consolidated_clicks.append(it_clicks[0])
            num_clicks.append(len(it_clicks))
        return consolidated_clicks, num_clicks, unique_image_ids

    def get_image_info(self, image_id):
        self.cur.execute(
            "select (image_path,syn_id,set_name) from images where _id=ANY(%s)", (image_id,))
        return [x['row'].replace(
            '(', '').replace(')', '').split(',') for x in self.cur.fetchall()]

    def get_image_paths(self, set_name):
        self.cur.execute(
            "SELECT image_path FROM images WHERE set_name=%s", (set_name,))
        return [x['image_path'] for x in self.cur.fetchall()]


def get_ims(set_name='ilsvrc2012train'):
    with db(g15_credentials()) as db_conn:
        images = db_conn.get_image_paths(set_name)
    return images


def get_data(exclude=None):
    with db(g15_credentials()) as db_conn:
        clicks = np.asarray(db_conn.get_click_coors(exclude=exclude))
        consolid_clicks, num_clicks, uni_image_ids = db_conn.consolidate_with_ids(
            deepcopy(clicks))
        image_info = db_conn.get_image_info(uni_image_ids.tolist())
        total_image_info = [
            db_conn.get_image_info([x['image_id']]) for x in clicks]
        image_types = np.unique(np.asarray([x[-1] for x in image_info]))
        # ilsvrc_train = db_conn.get_image_paths('ilsvrc2012train')
        # nsf_train_train = db_conn.get_image_paths('nsf')
        # train_images = flatten([ilsvrc_train, nsf_train_train])
    return {
               'clicks': clicks,
               'consolidated_clicks': consolid_clicks,
               'num_clicks': num_clicks,
               'unique_image_ids': uni_image_ids,
               'image_info': image_info,
               'total_image_info': total_image_info,
               'image_types': image_types,
               # 'ilsvrc_train': ilsvrc_train,
               # 'nsf_train_train': nsf_train_train,
               # 'train_images': train_images
           }


def randomization_test(ir_mirc_clickmaps, iterations=1000):
    corrs = np.zeros((iterations))
    for idx in tqdm(range(iterations)):
        it_corrs = np.zeros((len(ir_mirc_clickmaps)))
        for il, ic in enumerate(ir_mirc_clickmaps):
            # Random split half
            split_vec = np.random.permutation(ic)
            fh = np.mean(np.asarray(
                split_vec[:len(split_vec)//2]), axis=0).ravel()
            sh = np.mean(np.asarray(
                split_vec[len(split_vec)//2:]), axis=0).ravel()
            it_corrs[il] = spearmanr(fh, sh).correlation
        corrs[idx] = np.mean(it_corrs)
    corr = np.mean(corrs)
    p_value = 0
    return corr, p_value


def create_dir(d):
    if not os.path.exists(d):
        os.makedirs(d)


def create_subject_images(
        data,
        investigate_subject,
        subject_dir,
        image_dir,
        im_size=[256, 256],
        kernel=9,
        scoring='uniform',
        ext='.png'):
    this_sub_dir = os.path.join(subject_dir, investigate_subject)
    create_dir(this_sub_dir)
    if investigate_subject is None:
        subject_clicks = [data['clicks'][x] for x in range(
            len(data['clicks']))]
        investigate_subject = -1
    else:
        subject_clicks = [data['clicks'][x] for x in range(
            len(data['clicks']))
                if data['clicks'][x]['user_id'] == investigate_subject]
    if len(subject_clicks) > 0:
        print 'Creating clickmaps for subject: %s' % investigate_subject
        subject_cms = [create_heatmap(
            x['clicks'],
            im_size, kernel, scoring)
                for x in subject_clicks]
        with db(g15_credentials()) as db_conn:
            si_ids = [x['image_id'] for x in subject_clicks]
            image_names = [db_conn.get_image_info([x]) for x in si_ids]
        image_names = flatten(image_names)
        subject_images = [misc.imread(
            os.path.join(image_dir, x[0])) for x in image_names]
        [plot_image_hm(
            im,
            hm,
            os.path.join(
                this_sub_dir, '%s%s' % (idx, ext)))
            for idx, (im, hm) in tqdm(
                enumerate(
                    zip(reversed(subject_cms),
                        reversed(subject_images))),
                total=len(
                    subject_cms))]
    else:
        print 'Couldn\'t find any clickmap for subject: %s' % investigate_subject


def main(
        num_mosaic_ims=1000,
        mirc_output = '/home/drew/Documents/MIRC_behavior/click_comparisons/heatmaps_for_paper/clickme_mirc_heatmaps',
        hm_mosaic_output = '/home/drew/Documents/MIRC_behavior/click_comparisons/clickme_mosaic_hm.png',
        im_mosaic_output = '/home/drew/Documents/MIRC_behavior/click_comparisons/clickme_mosaic_im.png',
        image_dir = '/media/data_cifs/clicktionary/webapp_data',
        investigate_subjects = None,  # ['BklslAOgb'],  # ['HkCapZUql'],  # , 'S1EO1r_b-'# ['r1Ieyvtax'],  # relive_this@live.com ; ['rykyZPzTl'], freemoneyq ['rJkI8aOox'], # 
        exclude_subjects = ['r1Ieyvtax', 'B1x-611pl'],  # ['HkCapZUql'],  # , 'S1EO1r_b-'# ['r1Ieyvtax'],  # relive_this@live.com ; ['rykyZPzTl'], freemoneyq ['rJkI8aOox'], # 
        subject_dir = '/home/drew/Documents/MIRC_behavior/click_comparisons/clickme_subjects'):  # Famous Hole!!
    # Generate MIRC heatmaps
    data = get_data()

    if investigate_subjects is not None:
        create_dir(subject_dir)
        [create_subject_images(
            data=data,
            investigate_subject=inv_sub,
            image_dir=image_dir,
            subject_dir=subject_dir) for inv_sub in investigate_subjects]

    # Prepare data
    labels = np.asarray([x[2] for x in data['image_info']]) == 'mirc'
    mirc_data = np.asarray(data['consolidated_clicks'])[labels]
    mirc_clicks = np.asarray(data['num_clicks'])[labels]
    print 'Number of subjects: %s' % mirc_clicks
    mirc_clickmaps = [create_heatmap(
        x['clicks'], [256, 256], 9, 'uniform') for x in mirc_data]

    # Prepare images for mosaic
    sort_idx = np.argsort(data['num_clicks'])[::-1][:num_mosaic_ims]
    top_maps = [create_heatmap(
        data['consolidated_clicks'][x]['clicks'], [256, 256], 9, 'uniform')
            for x in sort_idx]
    original_images = [misc.imread(
        os.path.join(image_dir, data['image_info'][x][0])) for x in sort_idx]
    save_mosaic(maps=top_maps, output=hm_mosaic_output)
    save_mosaic(maps=original_images, output=im_mosaic_output)
    print 'Saved mosaics %s, %s' % (hm_mosaic_output, im_mosaic_output)

    # Measure interrater reliability
    trimmed_total_info = [x[0] for x in data['total_image_info']]
    ir_labels = np.asarray([x[2] for x in trimmed_total_info]) == 'mirc'
    ir_mirc_data = np.asarray(data['clicks'])[ir_labels]
    ir_mirc_labels = np.asarray(data['total_image_info'])[ir_labels]
    ir_mirc_names = np.asarray(
        [re.search('\d+', x[0][0]).group()
            for x in ir_mirc_labels]).astype(int)
    ir_mirc_clickmaps = []
    for idx in np.unique(ir_mirc_names):
        it_mirc = ir_mirc_data[ir_mirc_names == idx]
        ir_mirc_clickmaps.append(  # 9 corresponds to clicktionary
            [create_heatmap(x['clicks'], [256, 256], 9, 'uniform')
                for x in it_mirc])
    ir_mirc_clickmaps = [np.asarray(x) for x in ir_mirc_clickmaps]
    corr, p_value = randomization_test(ir_mirc_clickmaps)
    print 'MIRC interrater reliability: r = %s' % corr

    # Prepare to output
    im_names = np.asarray(
        [re.split(
            '.JPEG', re.split(
                '/', x[0])[1])[0] + '.npy'
            for x in data['image_info']])[labels]
    status = [np.save(
        os.path.join(
            mirc_output, f), x) for f, x in zip(
        im_names, mirc_clickmaps)]
    print 'Saved clickme heatmaps to MIRC_behavior project folder'
    return status


if __name__ == '__main__':
    main()