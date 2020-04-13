import tkinter as tk
import math
import time
import threading

import numpy as np
from sklearn.neighbors import NearestNeighbors
import networkx as nx
from UDPComms import Subscriber,timeout


class Transform:
    def __init__(self, matrix):
        self.matrix = matrix

    @classmethod
    def fromOdometry(cls, angle, xy):
        matrix = np.eye(3)
        matrix[0,0] = np.cos(angle); matrix[0,1] =-np.sin(angle)
        matrix[1,0] = np.sin(angle); matrix[1,1] = np.cos(angle)
        matrix[:2,2] = xy

        return cls(matrix)

    @classmethod
    def fromComponents(cls, angle, xy = None):
        if xy == None:
            xy = np.zeros((2))
        else:
            xy = np.array(xy)
        return cls.fromOdometry(np.radians(angle), xy)

    def combine(self, other):
        return Transform(self.matrix @ other.matrix)

    def inv(self):
        R = self.matrix[:2, :2]
        matrix = np.eye(3)
        matrix[:2,:2] = np.linalg.inv(R)
        matrix[:2,2]  = np.linalg.inv(R) @ self.matrix[:2, 2]
        return Transform(matrix)

    def get_components(self):
        x,y = self.matrix[:2,:2] @ np.array([1,0])
        angle = np.arctan2(y,x)
        return (angle, self.matrix[:2, 2])

    def copy(self):
        return Transform(self.matrix)


class Robot:
    def __init__(self, xy = (0,0), angle = 0):
        self.tranform = Transform.fromComponents(angle, xy)

    def drive(self, tranform):
        #local move
        self.tranform = self.tranform.combine(tranform)

    def move(self, tranform):
        #global move
        self.tranform = tranform.combine(self.tranform)

    def get_transform(self):
        return self.tranform

    def get_pose(self):
        pos = np.array([0,0,1])
        head = np.array([0,1,1])

        pos  = self.tranform.matrix @ pos
        head = self.tranform.matrix @ head - pos
        return (pos[:2], head[:2])

    def copy(self):
        return Robot(self.tranform.copy())

class PointCloud:
    def __init__(self, array):
        self.points = array

    def copy(self):
        return PointCloud(self.points.copy())

    @classmethod
    def fromScan(cls, scan):
        # from y axis clockwise
        scan = np.array(scan)
        angles = np.radians(scan[:,1])
        dists = scan[:,2]
        array = np.stack([dists*np.sin(angles), dists*np.cos(angles), np.ones(angles.shape)], axis=-1)
        return cls( array )

    def move(self, tranform):
        # print("matrix", tranform.matrix.shape)
        # print("self", self.points.shape)
        return PointCloud( (tranform.matrix @ self.points.T).T )

    def extend(self, other):
        MIN_DIST = 100

        nbrs = NearestNeighbors(n_neighbors=2).fit(self.points)

        # only middle (high resolution) points are valid to add
        print("other", other.points.shape)
        ranges = (other.points - np.mean(other.points, axis=0))[:, :2]
        ranges = np.sum(ranges**2, axis=-1)**0.5
        # print(ranges)
        points = other.points[ ranges < 2500, :]

        if points.shape[0] == 0:
            return

        distances, indices = nbrs.kneighbors(points)
        
        # print("distances", distances.shape)
        distances = np.mean(distances, axis=-1)
        matched_other = points[distances > MIN_DIST, :]

        self.points = np.vstack( (self.points, matched_other) )

    def fitICP(self, other):
        # TODO: better way of terminating
        transform = Transform.fromComponents(0)
        for itereation in range(5):
            aligment = self.AlignSVD(other)
            if aligment is None:
                return None, transform

            angle, xy = aligment.get_components()
            dist = np.sum(xy**2)**0.5

            if( np.abs(angle) > 0.3 or dist > 300 ):
                print("sketchy", itereation, angle, dist)
                return None, transform

            transform = aligment.combine(transform)
            other = other.move(aligment)

            if( angle < 0.001 and dist < 1 ):
                print("done", itereation)
                angle, xy = transform.get_components()
                dist = np.sum(xy**2)**0.5
                print("angle", angle, "Xy", xy)
                if( np.abs(angle) > 0.3 or dist > 300):
                    print("sketchy", itereation, angle)
                    return None, Transform(np.eye(3))
                return other, transform
        else:
            print("convergence failure!")
            return None, transform


    def AlignSVD(self, other):
        # other is the one moving
        MAX_DIST = 300

        # keep around
        nbrs = NearestNeighbors(n_neighbors=1).fit(self.points)
        distances, indices = nbrs.kneighbors(other.points)

        distances = np.squeeze(distances)
        indices = np.squeeze(indices)

        matched_indes = indices[distances <= MAX_DIST]
        matched_other = other.points[distances <= MAX_DIST, :]
        matched_self  = self.points[matched_indes, :]

        if matched_self.shape[0] < 10:
            print("not enough matches")
            return None

        self_mean = np.mean(matched_self, axis=0)
        other_mean = np.mean(matched_other, axis=0)

        matched_self = matched_self- self_mean
        matched_other = matched_other - other_mean

        M = np.dot(matched_other.T,matched_self)
        U,W,V_t = np.linalg.svd(M)

        R = np.dot(V_t.T,U.T)

        #consequence of homogeneous coordinates
        assert R[0,2] == 0
        assert R[1,2] == 0
        assert R[2,2] == 1
        assert R[2,0] == 0
        assert R[2,1] == 0
        
        t = self_mean - other_mean
        R[:2,2] = t[:2]
        
        return Transform(R)


class Vizualizer(tk.Tk):
    def __init__(self, size = 1000, mm_per_pix = 15):
        super().__init__()
        self.SIZE = size
        self.MM_PER_PIX = mm_per_pix

        self.canvas = tk.Canvas(self,width=self.SIZE,height=self.SIZE)
        self.canvas.pack()
        
    def delete(self, item):
        if hasattr(item, "tkiner_canvas_ids"):
            for obj in item.tkiner_canvas_ids:
                self.canvas.delete(obj)
        item.tkiner_canvas_ids = []

    def plot_PointCloud(self, pc, c='#000000'):
        self.delete(pc)

        for x, y,_ in pc.points:
            point = self.create_point(x, y, c=c)
            pc.tkiner_canvas_ids.append(point)

    def plot_Robot(self, robot, c="#FF0000"):
        self.delete(robot)

        pos, head = robot.get_pose()
        head *= 20

        arrow = self.canvas.create_line(self.SIZE/2 + pos[0]/self.MM_PER_PIX,
                           self.SIZE/2 - pos[1]/self.MM_PER_PIX,
                           self.SIZE/2 + pos[0]/self.MM_PER_PIX + head[0],
                           self.SIZE/2 - pos[1]/self.MM_PER_PIX - head[1],
                           arrow=tk.LAST)

        oval = self.canvas.create_oval(self.SIZE/2+5 + pos[0]/self.MM_PER_PIX, 
                                self.SIZE/2+5 - pos[1]/self.MM_PER_PIX,
                                self.SIZE/2-5 + pos[0]/self.MM_PER_PIX,
                                self.SIZE/2-5 - pos[1]/self.MM_PER_PIX,
                                fill = c)

        robot.tkiner_canvas_ids = [oval, arrow]

    def create_point(self,x,y, c = '#000000', w= 1):
        return self.canvas.create_oval(self.SIZE/2 + x/self.MM_PER_PIX,
                                self.SIZE/2 - y/self.MM_PER_PIX,
                                self.SIZE/2 + x/self.MM_PER_PIX,
                                self.SIZE/2 - y/self.MM_PER_PIX, width = w, fill = c, outline = c)




class SLAM:
    def __init__(self):
        self.viz = Vizualizer()

        self.odom  = Subscriber(8810, timeout=0.2)
        self.lidar = Subscriber(8110, timeout=0.1)

        self.robot = Robot()
        self.update_time = time.time()
        self.odom_transform = Transform.fromComponents(0)

        self.keyframes = []
        self.scan = None #most recent keyframe
        self.lidar_scan = None

        self.running = True
        self.threads = []
        self.threads.append( threading.Thread( target = self.update_odom, daemon = True) )
        self.threads.append( threading.Thread( target = self.update_lidar, daemon = True) )
        for thread in self.threads:
            thread.start()

        self.viz.after(100,self.update_viz)
        self.viz.mainloop()


    def update_viz(self):
        try:
            self.viz.plot_Robot(self.robot)

            if self.scan is not None:
                self.viz.plot_PointCloud(self.scan)
            if self.lidar_scan is not None:
                self.viz.plot_PointCloud(self.lidar_scan, c="blue")

            self.running = all([thread.is_alive() for thread in self.threads])
        except:
            self.running = False
            raise
        self.viz.after( 100 , self.update_viz)


    def update_odom(self):
        dt = 0.1
        while self.running:
            try:
                da, dy = self.odom.get()['single']['odom']
            except timeout:
                # print("odom timeout")
                continue

            da *= dt
            dy *= dt
            t = Transform.fromOdometry(da, (0,dy))
            self.odom_transform = t.combine(self.odom_transform)

            time.sleep(dt)
                


    def update_lidar(self):
        dt = 0.1
        while self.running:
            try:
                scan = self.lidar.get()
            except timeout:
                # print("lidar timeout")
                continue

            pc = PointCloud.fromScan(scan)

            # lidar in robot frame
            pc = pc.move(Transform.fromComponents(0, (-100,0) ))
            pc = pc.move( self.robot.get_transform() )
            pc.location = self.robot.get_transform()

            if len(self.keyframes) == 0:
                self.keyframes.append(pc)
                self.scan = pc
                self.lidar_scan = pc.copy()
                continue

            #hack for now
            self.lidar_scan.points = pc.copy().points
            cloud, transform = self.scan.fitICP(pc)

            robot = self.robot.get_transform().get_components()[1]
            scan  = self.scan.location.get_components()[1]

            if cloud is not None:
                self.robot.move(transform)
                if np.linalg.norm(robot - scan) > 500:
                    print("new keyframe")
                    self.scan = pc.move(transform)
                    self.scan.location = self.robot.get_transform()
                    self.keyframes.append( self.scan )

            self.robot.drive(self.odom_transform)
            self.odom_transform = Transform.fromComponents(0)

            time.sleep(dt)



if __name__ == "__main__":
    s = SLAM()

    # v = Vizualizer()
    # s1 = PointCloud.fromScan(scan1).move(Transform.fromComponents(0, (400,0)))
    # s2 = PointCloud.fromScan(scan2).move(Transform.fromComponents(15, (400,0)))

    # v.plot_PointCloud(s1)
    # v.plot_PointCloud(s2, c="blue")

    # s3, transform = s1.fitICP(s2)
    # # v.plot_PointCloud(s3, c="green")

    # s4 = s2.move(transform)
    # v.plot_PointCloud(s4, c="green")

    # v.mainloop()


