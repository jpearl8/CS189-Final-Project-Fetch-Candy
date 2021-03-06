"""
Program to allow a robot to "Fetch Candy" from Dispensers when the ARTag number and base location are passed in 
:param: ARTag number, Base Station Number
:return: None

Julia Pearl, Juliet Nwagw Ume-Ezeoke
    
"""

import random 
import math
from math import radians, degrees
import cv2
import numpy as np
import sys

#imports for rospy
import rospy
from geometry_msgs.msg import Twist
import tf
from kobuki_msgs.msg import BumperEvent, CliffEvent, WheelDropEvent, Sound
from geometry_msgs.msg import PoseWithCovarianceStamped, Point, Quaternion, PointStamped
from sensor_msgs.msg import Image
from cv_bridge import CvBridge, CvBridgeError
from std_msgs.msg import Empty
from ar_track_alvar_msgs.msg import AlvarMarkers

# imports for other functions
import map_script
import move_script
import cool_math as cm 

# used for determining last number of 
# seen ARTags that are the same
from itertools import groupby

# valid ids for AR Tags
VALID_IDS = range(18)

# values for initializing home
Home = 1
LEFT = -1
RIGHT = 1

# states in self.park(); i.e. descriptions for self.state2
SEARCHING = 0
ZERO_X = 1
TURN_ALPHA = 2
MOVE_ALPHA = 3
MOVE_PERF  = 4
SLEEPING = 5
BACK_OUT = 6
DONE_PARKING = 7
SEARCHING_2 = -1

class Main2:
    def __init__(self):
        # information about the robot's current position 
        # and orientation relative to start
        self.position = [0, 0]
        self.orientation = radians(0) # CCW +, radians 
        
        # mapping object will come from imported module 
        self.mapper = map_script.MapMaker()
        self.mapper.position = self.position
        self.mapper.orientation = self.orientation

        # move commands come from imported module 
        self.mover = move_script.MoveMaker()

        # for obstacle handling 
        self.obstacle = False
        
        # tells what side of the robot an 
        # obstacle is on 
        self.obs_side = 0 # left -1, right 1

        # states: wait, go_to_pos, go_to_AR, handle_AR
        self.state = 'wait'
        self.prev_state = 'wait'

        # used in park() to decrease confusion, see descriptions above
        self.state2 = None 

        # depth image for getting obstacles
        self.depth_image = []

        # ---- ARTag Parameters ----
        # tells when an ARTag has been seen 
        self.AR_seen = False

        # key of AR_TAG we are seeking
        self.AR_curr = -1

        # dictionary that stores information about current ARTag
        self.markers = {}

        # dictionary for ar ids and coordinates, second number how close robot needs to be from ar tag
        self.AR_ids = {
            1: [(0, 0), 0.9],
            11: [(-13, -1), 1.5],
            2: [(-2, -9), 1,],
            3: [(-18, -9), 0.8],
            4: [(-31, -1), 1],
            51: [(-30, 1), 1.5], #fake location to get around table
            5: [(-23, 10), 1.5],
            61: [(-31, 8), 1.5], #fake location to get around table
            6: [(-15, 7), 0.9],
            7: [(-9, 10), .75]}


        # vector orientation of ARTag relative to robot 
        # (usually an obtuse angle)
        self.ar_orientation = 0 # radians 

        # length of 'arm' between robot and ARTag 
        # when 0, robot is pointing at ARTag directly
        self.ar_x = 0 # m

        # distance between robot and ARTag 
        self.ar_z = 0 # m

        # if there's an obstacle and we are really 
        # close to the ar_tag, it's probably another robot
        self.close = False 

        # if we are extremely close to the ar_tag, we are 
        # just going to park or get bumped
        self.close_VERY = False
            
        # ---- Rospy Parameters ----
        # Initialize the node
        rospy.init_node('Main2', anonymous=False)

        # Tell user how to stop TurtleBot
        rospy.loginfo("To stop TurtleBot CTRL + C")
        # What function to call when you ctrl + c    
        rospy.on_shutdown(self.shutdown)

        # Subscribe to topic for AR tags
        rospy.Subscriber('/ar_pose_marker', AlvarMarkers, self.process_ar_tags)

        # Create a publisher which can "talk" to TurtleBot wheels and tell it to move
        self.cmd_vel = rospy.Publisher('wanderer_velocity_smoother/raw_cmd_vel',Twist, queue_size=10)

        # Subscribe to robot_pose_ekf for odometry/position information
        rospy.Subscriber('/robot_pose_ekf/odom_combined', PoseWithCovarianceStamped, self.process_ekf)

        # Set up the odometry reset publisher (publishing Empty messages here will reset odom)
        reset_odom = rospy.Publisher('/mobile_base/commands/reset_odometry', Empty, queue_size=1)
        # Reset odometry (these messages take about a second to get through)
        self.state_change_time = rospy.Time.now()
        timer = rospy.Time.now()
        while rospy.Time.now() - timer < rospy.Duration(1) or self.position is None:
            reset_odom.publish(Empty())

        # Use a CvBridge to convert ROS image type to CV Image (Mat)
        self.bridge = CvBridge()
        # Subscribe to depth topic
        rospy.Subscriber('/camera/depth/image', Image, self.process_depth_image, queue_size=1, buff_size=2 ** 24)

        # Subscribe to queues for receiving sensory data, primarily for bumps 
        rospy.Subscriber('mobile_base/events/bumper', BumperEvent, self.process_bump_sensing)
        self.sounds = rospy.Publisher('mobile_base/commands/sound', Sound, queue_size=10)

        # TurtleBot will stop if we don't keep telling it to move.  How often should we tell it to move? 5 Hz
        self.rate = rospy.Rate(5)

        
   
    def execute_command(self, my_move):
        """
        - Just a function to decrease repetion when executing move commands
        :param: a move command with linear and angular velocity set, see move_scipt.py
        :return: None
        """
        if (self.state is not "bumped" or self.state is not "avoid_obstacle"):
            move_cmd = my_move
            self.cmd_vel.publish(move_cmd)
            self.rate.sleep()
    
    def run(self):
        """
        - Control the state that the robot is currently in 
        - Run until Ctrl+C pressed
        :return: None
        """

        # get ar_tag desired from argument
        self.AR_curr = int(sys.argv[1])
        Home = int(sys.argv[2])
        
        
        # treat obstacle cases 5 and 6
        if (self.AR_curr == 5 or self.AR_curr == 6):
            self.AR_curr = self.AR_curr*10 + 1

        while not rospy.is_shutdown():
            move_cmd = Twist()

            #bumped or obstacle scenarios:
            if (self.state is "bumped" or self.state is "avoid_obstacle"):
                self.sounds.publish(Sound.ON)
                sec = 0

                # bumped when not very close to ar_tag
                if (self.state == "bumped" and not self.close_VERY):
                    print "bump when not very close to ar_tag"
                    sec = 5

                # bump when very close to ar_tag
                elif (self.state == "bumped" and self.close_VERY):
                    print "obstacle when very close to ar_tag!!"
                    sec = 15

                # obstacle while ar_tag not spotted: while there is obs, turn then move forwards
                elif (self.state == "avoid_obstacle" and self.close == False):
                    while(self.obs_side is not 0):
                        for i in range (2):
                            self.execute_command(self.mover.avoid_obstacle(self.obs_side))
                        obs_side = 0
                    self.execute_command(self.mover.go_forward())
                    sec = 2
                    self.prev_state = 'avoid_obstacle'
                    self.state = "go_to_pos"

                # obstacle at point ar_tag spotted
                else:
                    print "obstacle, moderately close to ar tag"
                    sec = 5
                rospy.sleep(sec)
                self.prev_state = 'avoid_obstacle'
                self.state = "go_to_pos"
                

            # wait stage (beginning and end)
            if (self.state == 'wait'):
                # just wait around 
                self.close_VERY = True
                move_cmd = self.mover.wait()
                if (self.AR_curr != -1):
                    print "changing state to go_to_pos"
                    self.prev_state = 'wait'
                    self.state = 'go_to_pos'

            # go to ekf position
            if (self.state == 'go_to_pos'):
                orienting = True 

                # orienting stage 
                if (not(self.AR_seen) or self.ar_z >= self.AR_ids[self.AR_curr][1]):
            
                    # adjust angle to face EKF position
                    if (orienting):
                        pos = self.AR_ids[self.AR_curr][0]
                        dest_orientation = cm.orient(self.mapper.positionToMap(self.position, self.AR_ids[Home][0]), pos)
                        angle_dif = cm.angle_compare(self.orientation, dest_orientation)
                        if (abs(float(angle_dif)) < abs(math.radians(5)) and self.state is not "bumped"):
                            self.close_VERY = False  
                            move_cmd = self.mover.go_to_pos("forward", self.position, self.orientation)
                            orienting = False
                            time = 3
                            if (self.AR_seen):
                                time = 1
                            for i in range (time):
                                self.execute_command(move_cmd)
                        else:
                            
                            # Turn in the relevant direction
                            if angle_dif < 0:
                                move_cmd = self.mover.go_to_pos("left", self.position, self.orientation) 
                            else:
                                move_cmd = self.mover.go_to_pos("right", self.position, self.orientation)
                                
                            self.execute_command(move_cmd)
                            
                    if (not orienting):
                        # big ar tag means edge case: 5/6 going to or returning from ar_tag
                        if ((self.AR_curr > 10)):
                            print "big ar tag"
                            travel_time = 100
                            issue = False
                            if (self.AR_curr == (Home*10) + 1):
                                travel_time = 20 
                            for i in range(travel_time):
                                if (self.state is not "bumped" or self.state is not "avoid_obstacle"):  
                                    move_cmd = self.mover.go_to_pos("forward", self.position, self.orientation)
                                    self.execute_command(move_cmd)
                                else:
                                    issue = True
                                    break
                            if (not issue):
                                self.AR_curr = (self.AR_curr - 1) / 10
                                orienting = True
            
                # when ar is seen and robot is close enough, change states
                if (self.AR_seen and self.ar_z < self.AR_ids[self.AR_curr][1]):
                    print "see AR"
                    self.sounds.publish(Sound.ON)
                    self.prev_state = 'go_to_pos'
                    self.state = 'go_to_AR'
            
            # go to the ARTag
            if (self.state == "go_to_AR"): 

                # set parameters for obstacle avoidance during parking 
                self.close = False
                self.close_VERY = True

                # set the initial state for the parking sequence and call park
                self.state2 = SEARCHING
                park_check = self.park()

                # only continue with main run sequence if parking was succesful
                if park_check == -1:
                    print "parking unsuccesful - going back to go to pos"
                    self.AR_seen = False
                    self.state = 'go_to_pos'
                else:
                    # return from handle ar!
                    print "parking succesful"
                    self.AR_seen = False
                    
                    
                    if (self.AR_curr is not Home):
                        # edge cases:
                        if (self.AR_curr == 6 or self.AR_curr == 5):
                            self.AR_curr = (Home * 10) + 1
                        else:
                        # go home
                            self.AR_curr = Home
                        self.prev_state = 'go_to_AR'
                        self.state = 'go_to_pos'
            
                    # already home
                    else:
                        self.AR_curr = -1
                        self.prev_state = 'go_to_AR'
                        self.state = 'wait'
                    self.cmd_vel.publish(move_cmd)
                    self.rate.sleep()



    def park(self):
        """
        - Control the parking that the robot does, has secondary control of the robot's state 
        :return: None
        """

        # goal distance between robot and ARTag before perfect parking 
        LL_DIST = 0.5 # m
        # distance between ARTag and robot when robot is almost touching it 
        CLOSE_DIST = 0.23 # m
        # desired accuracy when zeroing in on ARTag 
        X_ACC = 0.07 # m
        # parameters for limiting robots movement
        ALPHA_DIST_CLOSE = 0.01 # m
        ALPHA_RAD_CLOSE = radians(0.8) # radians

        # how long should the robot sleep under the dispenser
        SLEEP_TIME = 10
        # used to have to the robot oscillate when it is lost 
        OSC_LIM = 20

        # theshold for losing and finding the ARTag
        MAX_LOST_TAGS = 10
        MIN_FOUND_TAGS = 3

        # constants of proportionaly for setting speeds in self.park() only
        K_LIN = 0.25

        # distance between robot and parfet spot to park from 
        alpha_dist = 0 # m
        # radians between robot and angle to move 'alpha_dist'
        alpha = 0 # radians

        # used to determine how long to sleep 
        sleep_count = 0
        # determine robot velocity when lost
        osc_count = 0 
        # keep track of how long robot has been lost 
        lost_timer = None # seconds

        # boolean to move straight to ARTag at certain points
        almost_perfet = False

        # arrays to save information about robot's history 
        past_orr = []
        past_pos = []
        past_xs = []

        # just used for easier reading 
        ang_velocity = 0

        while not rospy.is_shutdown(): 
            while self.state is not "bumped" or self.state is not "avoid_obstacle":
                
                # only begin parking when the ARTag has been 
                # located and saved in markers dictionary
                if self.state2 is SEARCHING and len(self.markers) > 0:
                    print "in SEARCHING"

                    # used to decide what side of the robot the ARTag is on
                    theta_org = self.ar_orientation

                    # using the magnitude of the small angle 
                    # between the robot and ARTag, beta, for most calculations 
                    beta = abs(radians(180) - abs(theta_org))   
                    self.state2 = ZERO_X


                # handle event of ARTag being lost during the parking sequence
                # this has high priority over other states
                elif self.state2 is SEARCHING_2:
                    print "in SEARCHING 2 - ar tag lost"

                    # keep track of ar_x, which would only 
                    # be updated when ARTag is in view
                    del past_orr [:] # clear list of past positions
                    past_xs.append(self.ar_x) 

                    # if ar_x is being updated, then ARTag has been found, 
                    # return to parking
                    if len(past_xs) > MIN_FOUND_TAGS:
                        if not any(sum(1 for _ in g) > MAX_LOST_TAGS*0.5 for _, g in groupby(past_xs)):
                            print "found tag again!"
                            osc_count = 0 # clear counter for oscillations
                            del past_xs [:] # clear list of past ar_xs
                            self.state2 = ZERO_X
                    
                    # if the ARTag has been lost for too long, 
                    # return that parking was unsuccesful
                    if rospy.Time.now() - lost_timer > rospy.Duration(5):
                        print "cant find tag, going to return!"
                        return -1

                    # oscillate while looking for ARTag to 
                    # maximize chances of finding it again
                    osc_count+=1 
                    osc_count = osc_count % OSC_LIM
                    if osc_count < OSC_LIM * 0.5:  
                        self.execute_command(self.mover.twist(radians(-30)))
                    else:
                        self.execute_command(self.mover.twist(radians(30)))


                # turn to face the ARTag 
                if self.state2 is ZERO_X:
                    print "in zero x"

                    # keep track of whether the ARTag is still in view or is lost
                    past_xs.append(self.ar_x)
                    if any(sum(1 for _ in g) > MAX_LOST_TAGS for _, g in groupby(past_xs)):
                        lost_timer = rospy.Time.now() # track how long the ARTag has been lost 
                        self.state2 = SEARCHING_2
                        
                    
                    # turn until ar_x is almost 0
                    elif abs(self.ar_x) > X_ACC:
                        ang_velocity = self.ar_x * cm.prop_k_rot(self.ar_x)
                        self.execute_command(self.mover.twist(-ang_velocity))
                    
                    # triangulate distances and angles to guide 
                    # robot's parking and move to next state
                    else: 
                        # only want to move to the ARTag if parking sequence is almost complete
                        if almost_perfet == True:
                            almost_perfet = False
                            self.state2 = MOVE_PERF
                        else:    
                            alpha_dist = cm.third_side(self.ar_z, LL_DIST, beta) # meters
                            alpha = cm.get_angle_ab(self.ar_z, alpha_dist, LL_DIST) # radians
                            self.state2 = TURN_ALPHA


                # turn away from AR_TAG by a small angle alpha
                elif self.state2 is TURN_ALPHA:
                    print "in turn alpha"

                    # if robot is already close to ARTag, it should just park  
                    if self.ar_z <= CLOSE_DIST * 2.5: 
                        print "dont need to turn - z distance is low"
                        self.state2 = MOVE_PERF

                    # alpha will be exceptionally high when LL_DIST 
                    # is much greater than ar_z + alpha_dist - only need to park
                    elif abs(alpha) > 100:
                        print "dont need to turn - alpha is invalid"
                        self.state2 = MOVE_PERF
                    
                    # regular operation of just turning alpha
                    else: 
                        # keep track of how much robot has turned 
                        # since it entered 'alpha' state
                        past_orr.append(self.orientation)
                        dif =  cm.angle_compare(self.orientation,past_orr[0])
                        rad2go = abs(alpha) - abs(dif)

                        # want to always turn away from the ARTag until 
                        # robot has almost turned alpha
                        if rad2go > ALPHA_RAD_CLOSE: 
                            if theta_org < 0: # robot on left side of ARTag 
                                rad2go = rad2go * -1
                            # cm.prop_k_rot() helps the robot turn significantly 
                            # faster when rad2go is very small
                            ang_velocity = rad2go * cm.prop_k_rot(rad2go)
                            self.execute_command(self.mover.twist(ang_velocity)) 
                       
                        else:
                          del past_orr [:] # clear list of past orientations
                          self.execute_command(self.mover.wait())
                          self.state2 = MOVE_ALPHA


                # move to a position that makes parking convenient
                elif self.state2 == MOVE_ALPHA:
                    print "in move alpha"
                    # store info about ar_x as robot moves
                    past_xs.append(self.ar_x)

                    # keep track of how far robot has moved since it entered 'MOVE_ALPHA'
                    past_pos.append(self.position)
                    dist_traveled =  cm.dist_btwn(self.position, past_pos[0])
                    dist2go = abs(alpha_dist) - abs(dist_traveled)

                    # travel until the alpha_dist has been moved - need this to be very accurate
                    if dist2go > ALPHA_DIST_CLOSE and dist2go > CLOSE_DIST*2.5:
                        self.execute_command(self.mover.go_forward_K(K_LIN*alpha_dist))
                        print "dist2go in move alpha " + str(dist2go)
                    # dont need to move ALPHA_DIST anymore, robot is right up against AR_TAG
                    elif self.ar_z < CLOSE_DIST*2.5:
                         self.state2 = MOVE_PERF

                    # turn to face ARTag before moving directly to it 
                    else: 
                        del past_pos [:] # clear list of past positions
                        
                        # check if the ARTag data is valid before zeroing x
                        if any(sum(1 for _ in g) > MAX_LOST_TAGS*2 for _, g in groupby(past_xs)):
                            lost_timer = rospy.Time.now() # track how long the ARTag has been lost 
                            self.state2 = SEARCHING_2
                        # now zero ar_x 
                        elif abs(self.ar_x) > X_ACC:
                            self.state2 = ZERO_X
                            almost_perfet = True
                        else:
                            self.state2 = MOVE_PERF

                # move in a straight line to the ar tag 
                elif self.state2 == MOVE_PERF:
                    print "in move perf"

                    if self.ar_z < CLOSE_DIST * 3 and self.ar_z > CLOSE_DIST * 2:
                        print "ar_x" + str(self.ar_x)
                        if abs(self.ar_x) > X_ACC:
                            self.state2 = ZERO_X
                            almost_perfet = True

                    # move to the ARTag     
                    if self.ar_z > CLOSE_DIST:
                        self.execute_command(self.mover.go_forward_K(K_LIN*self.ar_z))
                    else:
                        # set parameters for avoiding obstacles
                        self.close = False
                        self.close_VERY = True

                        # don't need to sleep if at the home base
                        if (self.AR_curr is Home):
                            self.state = DONE_PARKING
                        else:
                            self.state2 = SLEEPING


                # wait to recieve package 
                elif self.state2 == SLEEPING:
                    print "in sleeping"

                    # use a counter to decide how long to sleep
                    sleep_count+=1
                    rospy.sleep(1)
                    if sleep_count > SLEEP_TIME:
                        self.state2 = BACK_OUT


                # back out from the ARTag
                elif self.state2 == BACK_OUT:  
                    print "in back out"

                    # reset EKF position using the ARTag 
                    self.position = self.mapper.positionFromMap(self.AR_ids[self.AR_curr][0], self.AR_ids[Home][0])
                    
                    # move backwards for a specific distance 
                    self.execute_command(self.mover.back_out())
                    if self.ar_z > CLOSE_DIST*3:
                        # set parameters for avoiding obstacles
                        self.close_VERY = False
                        self.state2 = DONE_PARKING


                # done with the parking sequence!
                elif self.state2 == DONE_PARKING:
                    print "in done parking"

                    self.execute_command(self.mover.wait())
                    # return succesful parking 
                    return 0

                self.rate.sleep()




    # ------------------ Functions reporting the robots interaction with the world ---------------- 
    def process_ar_tags(self, data):
        """
        Process the AR tag information.
        :param data: AlvarMarkers message telling you where multiple individual AR tags are
        :return: None
        """
        for marker in data.markers:
            if (marker.id == self.AR_curr):
                self.AR_seen = True
                self.close = True
                pos = marker.pose.pose.position # what is this relative to -- robot at that time - who is the origin 

                distance = cm.dist((pos.x, pos.y, pos.z))

                self.ar_x = pos.x
                # print "X DIST TO AR TAG %0.2f" % self.ar_x
                self.ar_z = pos.z

                orientation = marker.pose.pose.orientation
                list_orientation = [orientation.x, orientation.y, orientation.z, orientation.w]
                self.ar_orientation = tf.transformations.euler_from_quaternion(list_orientation)[0]
                self.markers[marker.id] = distance
        

    def process_ekf(self, data):
        """
        Process a message from the robot_pose_ekf and save position & orientation to the parameters
        :param data: PoseWithCovarianceStamped from EKF
        """
        # Extract the relevant covariances (uncertainties).
        # Note that these are uncertainty on the robot VELOCITY, not position
        cov = np.reshape(np.array(data.pose.covariance), (6, 6))
        x_var = cov[0, 0]
        y_var = cov[1, 1]
        rot_var = cov[5, 5]
        # You can print these or integrate over time to get the total uncertainty
        
        # Save the position and orientation
        pos = data.pose.pose.position
        extra_pos = [0, 0]
        extra_or = 0


        self.position = (pos.x + extra_pos[0], pos.y + extra_pos[1])
        orientation = data.pose.pose.orientation
        list_orientation = [orientation.x, orientation.y, orientation.z, orientation.w]
        self.orientation = tf.transformations.euler_from_quaternion(list_orientation)[-1] + extra_or


    #   OBSTACLE TWEAKING: the range of obstacle depth detected, the width of camera, area of obstacle      
    def bound_object(self, img_in):
        """
        - Draws a bounding box around the largest object in the scene and returns
        - Lets us know when obstacles have been seen
        - Lets us know when to avoid obstacles
        :param: Image described by an array
        :return: Image with bounding box 
        """
        img = np.copy(img_in)
        img = img[:-250, :]
        middle_seg = False
        img_height, img_width = img.shape[:2] # (480, 640) 

        # Get contours
        contours, _ = cv2.findContours(img, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        if len(contours) > 0:
            # Find the largest contour
            areas = [cv2.contourArea(c) for c in contours]
            max_index = np.argmax(areas)
            max_contour = contours[max_index]
            new_obstacle_pos = cm.centroid(max_contour)

            # show where largest obstacle is 
            cv2.drawContours(img, max_contour, -1, color=(0, 255, 0), thickness=3)
       
            # Draw rectangle bounding box on image
            x, y, w, h = cv2.boundingRect(max_contour)

            # obstacle must be even larger to get the state to be switched 
            if (w*h > 400):
                if (self.close_VERY == False):
                    if (x < 220):
                        self.obs_side = LEFT
                    else:
                        self.obs_side = RIGHT
                    print "avoiding obstacle"
                    self.prev_state = self.state
                    self.state = 'avoid_obstacle'
        return img

    def process_depth_image(self, data):
        """ 
        - Use bridge to convert to CV::Mat type. (i.e., convert image from ROS format to OpenCV format)
        - Displays thresholded depth image 
        - Calls bound_object function on depth image
        :param: Data from depth camera
        :return: None
        """
        try:
            cv_image = self.bridge.imgmsg_to_cv2(data)
            
            # create mask for range 0.1 - 0.5 meters, restricting image to be directly in front
            mask = cv2.inRange(cv_image, 0.1, 0.5)
            mask[:, 0:180] = 0
            mask[:, 460:] = 0
            # create a mask to restrict the depth that can be seen 
            im_mask = cv2.bitwise_and(cv_image, cv_image, mask=mask)
            self.depth_image = im_mask

            # bound the largest object within this masked image 
            dst2 = self.bound_object(mask)

            # Normalize values to range between 0 and 1 for displaying
            norm_img = im_mask
            cv2.normalize(norm_img, norm_img, 0, 1, cv2.NORM_MINMAX)

            # Displays thresholded depth image   
            # cv2.imshow('Depth Image', norm_img)    
            # cv2.waitKey(3)

        except CvBridgeError, err:
            rospy.loginfo(err)

    def process_bump_sensing(self, data):
        """
        Simply sets state to bump and lets other functions handle it
        :param data: Raw message data from bump sensor 
        :return: None
        """
        if (data.state == BumperEvent.PRESSED):
            self.prev_state = self.state
            self.state = 'bumped'

    def shutdown(self):
        """
        Pre-shutdown routine. Stops the robot before rospy.shutdown 
        :return: None
        """
        # Close CV Image windows
        cv2.destroyAllWindows()
        # stop turtlebot
        rospy.loginfo("Stop TurtleBot")
        # a default Twist has linear.x of 0 and angular.z of 0.  So it'll stop TurtleBot
        self.cmd_vel.publish(Twist())
        # sleep just makes sure TurtleBot receives the stop command prior to shutting down the script
        rospy.sleep(1)

if __name__ == '__main__':
    # try:
    robot = Main2()
    robot.run()
    print "success"

    # gives cleaner error descriptions
    except Exception, err:
        rospy.loginfo("You have an error!")
        print err
