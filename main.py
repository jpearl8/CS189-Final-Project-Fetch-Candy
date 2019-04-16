"""
Milestone 1: Fetch Candy
Main script controlling robot functionality
"""

import random
import math
from math import radians, degrees
import cv2
import numpy as np

imports for rospy
import rospy
from geometry_msgs.msg import Twist
import tf
from kobuki_msgs.msg import BumperEvent, CliffEvent, WheelDropEvent
from geometry_msgs.msg import PoseWithCovarianceStamped, Point, Quaternion, PointStamped
from sensor_msgs.msg import Image
from cv_bridge import CvBridge, CvBridgeError
from std_msgs.msg import Empty
from ar_track_alvar_msgs.msg import AlvarMarkers

#imports for other functions
import map_script
import move_script
import cool_math as cm 




class Main:
    def __init__(self):
        # mapping object will come from imported module 
        self.mapper = map_script.MapMaker()

        # moving object will come from imported module 
        self.mover = move_script.MoveMaker()

        # information about the robot's current position and orientation relative to start
        self.position = [0,0]
        self.orientation = 0 # CCW +, radians 
        # imported mapper will use this position and orientation 
        self.mapper.position = self.position
        self.mapper.orientation = self.orientation

        # the inistalized state is 'wander'
        self.state = 'wander'
        self.prev_state = None

        # depth image for getting obstacles
        self.depth_image = []

        # booleans that let robot know when and where to map
        self.obstacle_seen = False
        self.obstacle_depth = -1
        self.AR_seen = False

        # booleans that help the robot avoid obstacles and react to bumps 
        self.obstacle_side = None 

        # dictionary holding all ARTags ever seen
        # tag_id: distance pairs
        self.AR_q = {}
        # current AR_TAG we are concerned with 
        self.AR_curr = None
        # lets it be know that an ARTAG is very close 
        self.AR_close = False

        # # ---- rospy stuff ----
        # Initialize the node
        rospy.init_node('Main', anonymous=False)

        # Tell user how to stop TurtleBot
        rospy.loginfo("To stop TurtleBot CTRL + C")
        # What function to call when you ctrl + c    
        rospy.on_shutdown(self.shutdown)

        # Create a publisher which can "talk" to TurtleBot wheels and tell it to move
        self.cmd_vel = rospy.Publisher('cmd_vel_mux/input/navi',Twist, queue_size=10)

        # Subscribe to robot_pose_ekf for odometry/position information
        rospy.Subscriber('/robot_pose_ekf/odom_combined', PoseWithCovarianceStamped, self.process_ekf)

        # Set up the odometry reset publisher (publishing Empty messages here will reset odom)
        reset_odom = rospy.Publisher('/mobile_base/commands/reset_odometry', Empty, queue_size=1)
        # Reset odometry (these messages take about a second to get through)
        timer = rospy.Time.now()
        while rospy.Time.now() - timer < rospy.Duration(1) or self.position is None:
            reset_odom.publish(Empty())

        # Subscribe to topic for AR tags
        rospy.Subscriber('/ar_pose_marker', AlvarMarkers, self.process_ar_tags)

        # Use a CvBridge to convert ROS image type to CV Image (Mat)
        self.bridge = CvBridge()
        # Subscribe to depth topic
        rospy.Subscriber('/camera/depth/image', Image, self.process_depth_image, queue_size=1, buff_size=2 ** 24)

        # Subscribe to queues for receiving sensory data, primarily for bumps 
        rospy.Subscriber('mobile_base/events/bumper', BumperEvent, self.process_bump_sensing)

        # TurtleBot will stop if we don't keep telling it to move.  How often should we tell it to move? 5 Hz
        self.rate = rospy.Rate(5)

    def execute_move(self, range_max, a_move_cmd):
        """
        Will perform the movement passed in for the desired amount of time
        :param: range over which movement should be published, Twist() object
        :return: None
        """
        for i in range (0, range_max):
            self.cmd_vel.publish(a_move_cmd)
            self.rate.sleep() # sleep w same frequency as rest of program


    def run(self):
        """
        - Control the state that the robot is currently in 
        - Run until Ctrl+C pressed
        :return: None
        """
        # for testing on mac
        # i = 0
        while rospy.spin(): #replace with rospy.spin
            # print i
            # i+=1
            # one twist object will be shared by all the states 
            move_cmd = Twist()
             
            #move_cmd = None
            
            while (self.state == 'wander'):
                # just wandering around 
                move_cmd = self.mover.wander()
                self.cmd_vel.publish(move_cmd)
                self.rate.sleep()

                # current location will always be free :)
                self.mapper.updateMapFree()
                
                # this info will come from depth senor processing
                if (self.obstacle_seen == True):
                    self.mapper.updateMapObstacle()

                # map the ARTAG using info from ARTAG sensor stored in self
                elif (self.AR_seen == True): 
                    self.mapper.updateMapAR()

                # TODO: have a parameter under which the correct AR tag is chosen 
                elif (self.AR_q > 1):
                    self.AR_curr = self.mover.choose_AR() # should only return valid values if there is an AR to go to 
                    # robot will now go to AR tag 
                    self.prev_state = 'wander'
                    self.state = 'go_to_AR'
                    
                    
            # handle obstacles and bumps that interrupt work flow
            if (self.state == 'avoid_obstacle' or self.state == 'bumped'):
                if (self.state == 'bumped'):
                    move_cmd = self.mover.bumped()
                    #self.execute_move(10, self.mover.bumped())
                 
                    # return to previous state after bumping
                    # never want prev state to be reacting to a bump
                    self.state = self.prev_state
                else:
                    move_cmd = self.mover.avoid_obstacle() 
                    # self.execute_move(10, self.mover.avoid_obstacle())
                    # publish the move_cmd

                    # return to previous state
                    # never want prev state to be avoinding obstacles
                    self.state = self.prev_state

            elif (self.state == 'go_to_AR'):
                move_cmd = self.mover.go_to_AR()
                # self.execute_move(10, self.mover.go_to_AR())
                # publish the move_cmd
                if (self.AR_close == True):
                    self.prev_state = 'go_to_AR'
                    self.state == 'handle_AR'
                      
            elif (self.state == 'handle_AR'):
                move_cmd = self.mover.handle_AR()
                # self.execute_move(10, self.mover.handle_AR())
                # publish the move_cmd
                # pause for 10 seconds
                self.prev_state = 'handle_AR'
                self.state = 'wander'

# ------------------ Functions telling us about the robot ---------------- #     

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
        self.position = (pos.x, pos.y)
        orientation = data.pose.pose.orientation
        list_orientation = [orientation.x, orientation.y, orientation.z, orientation.w]
        self.orientation = tf.transformations.euler_from_quaternion(list_orientation)[-1]
            
    def process_ar_tags(self, data): # kind of ratch -- look in to!
        """
        Process the AR tag information.
        :param: data: AlvarMarkers message telling you where multiple individual AR tags are
        :return: None
        """
        # These are the only tag IDs that are being considered here
        VALID_IDS = range(18)

        # Set the position for all the markers that are in the received message
        for marker in data.markers:
            if marker.id in VALID_IDS:
                pos = marker.pose.pose.position
                #distance = cm.dist((pos.x, pos.y, pos.z)) # care more about their position than the distance 
                self.AR_q[marker.id] = pos
        # Set the position to None for all the previously seen markers that weren't in the message
        all_seen_ids = self.AR_q.keys()
        seen_now_ids = [m.id for m in data.markers]
        missing_ids = list(set(all_seen_ids) - set(seen_now_ids))
        # dont want this behavior atm
        self.AR_q.update(dict.fromkeys(missing_ids, None))

    
    def bound_object(self, img_in):
        """
        - Draws a bounding box around the largest object in the scene and returns
        - Lets us know when obstacles have been seen
        - Lets us know when to avoid obstacles
        :param: Image described by an array
        :return: Image with bounding box 
        """
        img = np.copy(img_in)

        # Get contours
        contours, _ = cv2.findContours(img, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        if len(contours) > 0:
            # Find the largest contour
            areas = [cv2.contourArea(c) for c in contours]
            max_index = np.argmax(areas)
            max_contour = contours[max_index]
            new_obstacle_pos = cm.centroid(max_contour)

            # returns obstacle depth that will allow obstacle to be mapped 
            if new_obstacle_pos:
                self.obstacle_depth =  self.depth_image[new_obstacle_pos[0]][new_obstacle_pos[1]] 

            # show where largest obstacle is 
            cv2.drawContours(img, max_contour, -1, color=(0, 255, 0), thickness=3)
       
            # Draw rectangle bounding box on image
            x, y, w, h = cv2.boundingRect(max_contour)

            # only want to map obstacle if it is large enough 
            if (w*h > 200):
                self.obstacle_seen = True

            # obstacle must be even larger to get the state to be switched 
            if (w*h > 400):
                self.state = 'avoid_obstacle'
                # Differentiate between left and right objects
                if (x < 160):  
                    self.obstacle_side = 'left'
                else:
                    self.obstacle_side = 'right'         

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

            mask = cv2.inRange(cv_image, 0.1, 1)
            
            # create a mask to restrict the depth that can be seen 
            mask[400:, :] = 0
            mask[:, 0:200] = 0
            mask[:, 440:] = 0
            im_mask = cv2.bitwise_and(cv_image, cv_image, mask=mask)
            self.depth_image = im_mask

            # bound the largest object within this masked image 
            dst2 = self.bound_object(mask)

            # Normalize values to range between 0 and 1 for displaying
            norm_img = im_mask
            cv2.normalize(norm_img, norm_img, 0, 1, cv2.NORM_MINMAX)

            # Displays thresholded depth image   
            cv2.imshow('Depth Image', norm_img)    
            cv2.waitKey(3)

        except CvBridgeError, err:
            rospy.loginfo(err)

    def process_bump_sensing(self, data):
        """
        Simply sets state to bump and lets other functions handle it
        :param data: Raw message data from bump sensor 
        :return: None
        """

        if (data.state == BumperEvent.PRESSED):
            self.state = 'bumped'

    def shutdown(self):
        """
        Pre-shutdown routine. Stops the robot before rospy.shutdown 
        :return: None
        """
        # TODO: save the map image - maybe put somewhere else?

        # Close CV Image windows
        cv2.destroyAllWindows()
        # stop turtlebot
        rospy.loginfo("Stop TurtleBot")
        # a default Twist has linear.x of 0 and angular.z of 0.  So it'll stop TurtleBot
        self.cmd_vel.publish(Twist())
        # sleep just makes sure TurtleBot receives the stop command prior to shutting down the script
        rospy.sleep(5)

if __name__ == '__main__':
    # try:
    robot = Main()
    robot.run()
    print "success"

    # uncomment for cleaner print outs!
    # except Exception, err:
    #     rospy.loginfo("You have an error!")
    #     print err
    
       

