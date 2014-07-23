#!/usr/bin/env python

# Software License Agreement (BSD License)
#
# Copyright (c) 2013, SRI International
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#  * Neither the name of SRI International nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
# Author: Acorn Pooley

## BEGIN_SUB_TUTORIAL imports
##
## To use the python interface to move_group, import the moveit_commander
## module.  We also import rospy and some messages that we will use.
import roslib
roslib.load_manifest("listen_and_grasp")

import sys
import copy
import rospy
import moveit_commander
import moveit_msgs.msg
import geometry_msgs.msg
import baxter_interface
import genpy
import random
import traceback
import math
import actionlib
import tf

## END_SUB_TUTORIAL

from std_msgs.msg import String, Header
from geometry_msgs.msg import PoseStamped
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
from control_msgs.msg import FollowJointTrajectoryGoal, FollowJointTrajectoryAction
from moveit_msgs.msg import Grasp
from object_recognition_msgs.msg import RecognizedObjectArray
from tf import TransformListener, LookupException, ConnectivityException, ExtrapolationException
from tf.transformations import quaternion_from_euler
from move_msgs.msg import moveAction, moveRegion
from baxter_core_msgs.srv import SolvePositionIK, SolvePositionIKRequest
#from meldon_detection.msg import MarkerObjectArray, MarkerObject
from baxter_grasps_server.srv import GraspService

from visualization_msgs.msg import Marker

class Place:
	def __init__(self):
		self.objects = []
		self.iksvc = None
		self.object_bounding_boxes = dict()
		self.objectPoses = dict()
		self.graspService = rospy.ServiceProxy('grasp_service', GraspService)
		self.scene = moveit_commander.PlanningSceneInterface()
		#self.robot = moveit_commander.RobotCommander()
		self.group = moveit_commander.MoveGroupCommander("left_arm")
		self.left_arm = baxter_interface.limb.Limb("left")
		
		self.limb_command = actionlib.SimpleActionClient("/robot/left_velocity_trajectory_controller/follow_joint_trajectory", FollowJointTrajectoryAction)
		self.limb_command.wait_for_server()
		self.transformer = TransformListener()
		
		self.markers_publisher = rospy.Publisher("/grasp_markers", Marker)
		self.is_picking = False
		self.is_placing = False

	def goToNeutral(self):
		pose_target = geometry_msgs.msg.PoseStamped()
		pose_target.header.frame_id = "/base"
		pose_target.pose.orientation.x = 0.
		pose_target.pose.orientation.y = 0.707
		pose_target.pose.orientation.z = 0
		pose_target.pose.orientation.w = 0.707
		pose_target.pose.position.x = 0.8
		pose_target.pose.position.y = 0.3
		pose_target.pose.position.z = 0.11
		self.group.set_pose_target(pose_target)
		self.group.plan()
		self.group.go()

	def interpolate(self, start, end):
		joint_arrays = []
		maxPoints = 2
		for name in start.keys():
				if name in end:
					diff = math.fabs(start[name] - end[name])
					numPoints = diff / 0.01
					if numPoints > maxPoints:
						maxPoints = int(numPoints)
		for i in range(maxPoints):
			t = i / 100.0
			joints = dict()
			for name in start.keys():
				if name in end:
					current = (1 - t)*start[name] + t * end[name]
					joints[name] = current
			joint_arrays.append(joints)
		return joint_arrays

	def moveToNeutral(self):
		trajectory = JointTrajectory()
		trajectory.header.stamp = rospy.Time.now()
		current_joints = self.left_arm.joint_angles()
		angles = dict(zip(self.left_arm.joint_names(),
                          [0.0, -0.55, 0.0, 0.75, 0.0, 1.26, 0.0]))
		joints = self.interpolate(current_joints, angles)
		index = 0
		for joint_dict in joints:
			index += 1
			point = JointTrajectoryPoint()
			point.time_from_start = rospy.rostime.Duration(0.1 * index)
			for name, angle in joint_dict.iteritems():
				if (index == 1):
					trajectory.joint_names.append(name)
				point.positions.append(angle)
			trajectory.points.append(point)
		trajectory.header.stamp = rospy.Time.now()
		goal = FollowJointTrajectoryGoal(trajectory=trajectory, goal_time_tolerance = rospy.rostime.Duration(5.0))
		rospy.loginfo("Moving left arm to neutral " + str(0.3 * len(joints)))
		self.limb_command.send_goal(goal)
		self.limb_command.wait_for_result()
		

	def addBoundingBox(self, points, name):
		minX = sys.float_info.max
		minY = sys.float_info.max
		minZ = sys.float_info.max
		maxX = -sys.float_info.max
		maxY = -sys.float_info.max
		maxZ = -sys.float_info.max

		for point in points:
			if (point.x() > maxX):
				maxX = point.x()
			if (point.y() > maxY):
				maxY = point.y()
			if (point.z() > maxZ):
				maxZ = point.z()
			if (point.x() < minX):
				minX = point.x()
			if (point.y() < minY):
				minY = point.y()
			if (point.z() < minZ):
				minZ = point.z()
		dim_x = maxX - minX
		dim_y = maxY - minY
		dim_z = maxZ - minZ

		pose = PoseStamped()
		pose.header.frame_id = "/base"
		pose.pose.position.x = (maxX + minX) / 2.0
		pose.pose.position.y = (maxY + minY) / 2.0
		pose.pose.position.z = (maxZ + minZ) / 2.0
		self.scene.add_box(name, pose, (dim_x, dim_y, dim_z))

	def addBoundingBoxAtPose(self, name):
		width = 0.03
		pose = self.objectPoses[name]
		pose.pose.position.z += 0.1
		self.object_bounding_boxes[name] = dict()
		self.object_bounding_boxes[name]["scale"] = [width, width, 0.2]
		self.object_bounding_boxes[name]["pose"] = pose
		self.scene.add_box(name, pose, (width, width, 0.2))

	def getPoseStampedFromPoseWithCovariance(self, pose):
		pose_stamped = PoseStamped()
		pose_stamped.header= copy.deepcopy(pose.header)
		pose_stamped.pose.position = copy.deepcopy(pose.pose.pose.position)
		pose_stamped.pose.position.z -= 0
		pose_stamped.pose.orientation = copy.deepcopy(pose.pose.pose.orientation)
		now = rospy.Time.now()
		self.transformer.waitForTransform("/world", pose_stamped.header.frame_id, rospy.Time(), rospy.Duration(4,0))
		pose_stamped.header.stamp = self.transformer.getLatestCommonTime("/world", pose_stamped.header.frame_id)
		transformedPose = self.transformer.transformPose("/world", pose_stamped)
		return transformedPose
		

	def objectsCallback(self, msg):
		if self.is_picking or self.is_placing:
			return
		for object in self.objects:
			self.scene.remove_world_object(object)
		self.objects = []
		self.objectPoses = dict()
		self.object_bounding_boxes = dict()
		for object in msg.objects:
			newPose = self.getPoseStampedFromPoseWithCovariance(object.pose)
			x = newPose.pose.position.x
			y = newPose.pose.position.y
			z = newPose.pose.position.z
			
			if x > 0.3 and y < 0.8 and y > -0.8 and z > -0.3:
				rospy.loginfo(newPose)
				self.objects.append(object.type.key)
				self.objectPoses[object.type.key] = newPose
				self.addBoundingBoxAtPose(object.type.key)

	def burlapObjectRequestCallback(self, msg):
		if self.is_picking or self.is_placing:
			return
		

		object_name = msg.object.name
		object_id = msg.object.hashID

		if object_id not in self.objects:
			rospy.logerr("Object " + object_id + " (" + object_name +  ") is not in detected objects ")
			
			object_str = ""
			for object in self.objects:
				object_str += ", " + str(object)
			rospy.logerr("Detected objects " + object_str)

			return

		rospy.loginfo("Getting grasp for object " + object_name)
		graspResponse = self.graspService(object_name)
		if not graspResponse.success:
			rospy.logerr("No grasps were found for object " + object_name)
			return

		rospy.loginfo("Finding a valid place pose")
		place_pose = self.getValidPlacePose(msg.region, msg.header.frame_id, object_id)
		if place_pose == None:
			rospy.logerror("Place region is invalid")
			return

		rospy.loginfo("place pose frame id " + str(place_pose.header.frame_id))
		rospy.loginfo("object pose frame id " + str(self.objectPoses[object_id].header.frame_id))
		
		rospy.loginfo("Attempting to pick up object " + object_name)
		self.is_picking = True
		pickSuccess = False
		try:
			pickSuccess = self.pick(object_name, object_id)
		except Exception as e:
			traceback.print_exc()
			raise e
		finally:
			self.is_picking = False

		if not pickSuccess:
			rospy.logerr("Object pick up failed")
			return

		self.is_placing = True
		placeSuccess = False
		placeCount = 0
		while not placeSuccess and placeCount < 10:
			rospy.loginfo("Attempting to place object")
			placeCount = placeCount + 1
			try:
				placeSuccess = self.place(object_id, place_pose)
			except Exception as e:
				traceback.print_exc()
				raise e
			finally:
				self.is_placing = False
		

	def objectRequestCallback(self, msg):
		if msg.data not in self.objects:
			rospy.logerr("Object " + msg.data + " is not in detected objects")
			return
		
		graspResponse = self.graspService(msg.data)
		if not graspResponse.success:
			rospy.logerr("No grasps were found for object " + msg.data)
			return

		self.group.set_start_state_to_current_state()
		robot.left_arm.pick(msg.data, graspResponse.grasps)

	def addTable(self):
		scene = moveit_commander.PlanningSceneInterface()
		p = PoseStamped()
 		p.header.frame_id = "/base"
   		p.pose.position.x = 0.35  
  		p.pose.position.y = 0
  		p.pose.position.z = -0.75
  		scene.add_box("table", p, (2.1, 2.0, 1.0))#0.35

	def pick(self, pose, object_name):
		self.group.detach_object()			

		graspResponse = self.graspService(object_name)
		if not graspResponse.success:
			rospy.logerr("No grasps were found for object " + object_name)
			return

		self.group.set_planning_time(20)
		self.group.set_start_state_to_current_state()

		grasps = self.setGrasps(object_name, pose, graspResponse.grasps)
		self.publishMarkers(grasps, object_name)
		
		result = self.group.pick(object_name, grasps * 5)
		return result

	def attachObject(self, name):
		width = 0.03
		pose = PoseStamped()
		pose.header.frame_id = "world"
		pose.pose.position.x = 0.631
		pose.pose.position.y = 0.838
		pose.pose.position.z = 0.043
		pose.pose.orientation.y = 0.707
		pose.pose.orientation.w = 0.707
		self.scene.add_box(name, pose, (width, width, 0.2))
		#self.group.attach_object(name)
		return pose

	def place(self, object_id, place_pose):
		result = self.group.place(object_id, place_pose)
		return result

	def getValidPlacePoses(self):
		place_poses = []
		for i in range(36):
			place_pose = PoseStamped()
			place_pose.header.frame_id = "world"
			place_pose.pose.position.x = 0.6
			place_pose.pose.position.y = 0.3
			place_pose.pose.position.z = 0.0
			quat = quaternion_from_euler(0, math.pi/2.0, i * 2.0 * math.pi / 36.0)
			rospy.loginfo(str(quat))
			place_pose.pose.orientation.x = quat[0]
			place_pose.pose.orientation.y = quat[1]
			place_pose.pose.orientation.z = quat[2]
			place_pose.pose.orientation.w = quat[3]
			place_poses.append(place_pose)
		return place_poses

	def getUnnocupiedCells(self, collision_map):
		unnocupied_cells = []
		for y in range(len(collision_map)):
			for x in range(len(collision_map[y])):
				if (collision_map[y][x] == 0):
					unnocupied_cells.append([x, y])
		return unnocupied_cells

	def getCollisionMap(self, collision_region, object_id, radius):
		distance_from_edge = copy.deepcopy(collision_region)
		for row in range(len(collision_region)):
			for column in range(len(collision_region[row])):
				occupancy = collision_region[row][column]
				if (occupancy > 0):
					for column_prime in range(int(column - radius), int(math.ceil(column + radius))):
						if column_prime >= 0 and column_prime < len(collision_region[row]):
							distance = math.fabs(column_prime - column)
							distance_from_edge[row][column_prime] = max(radius - distance, 0)

		reduced_collision_region = copy.deepcopy(distance_from_edge)
		for row in range(len(distance_from_edge)):
			for column in range(len(distance_from_edge[row])):
				distance = distance_from_edge[row][column]
				if (distance > 0):
					for row_prime in range(int(row - radius), int(math.ceil(row + radius))):
						if row_prime >= 0 and row_prime < len(distance_from_edge):
							column_distance = radius - distance_from_edge[row][column]
							row_distance = math.fabs(row_prime - row)
							distance_squared = column_distance * column_distance + row_distance * row_distance
							distance = math.sqrt(distance_squared)
							reduced_collision_region[column][row_prime] = max(radius - distance, 0)


		#full_collision_map = copy.deepcopy(collision_region)

		#for object in self.objects:
		#	if object != object_id:
		#		bounding_box = self.object_bounding_boxes[object]
		return [[min(1, value) for value in row] for row in reduced_collision_region]

	def getOpenCollisionRegion(self, move_region):
		region_width = move_region.scale.x * 100 #convert meters into cm squares
		region_height = move_region.scale.y * 100
		region_x = move_region.origin.x
		region_y = move_region.origin.y

		validity_function = {
			moveRegion.SHAPE_SQUARE: lambda x, y: math.fabs(x - region_x) <= region_width / 2.0 and math.fabs(y - region_y) <= region_height,
			moveRegion.SHAPE_CIRCLE: lambda x, y: (x - region_x)*(x - region_x) / region_width + (y - region_y)*(y - region_y) / region_height <= 1.0
		}

		isValid = validity_function[move_region.shape]

		return [[int(isValid(x, y)) for x in range(100)] for y in range(100)]


	def setGrasps(self, name, pose, grasps):
		correctedGrasps = []
		index = 0
		for grasp in grasps:
			newGrasp = copy.deepcopy(grasp)
			newGrasp.id = str(index)
			index += 1
			newGrasp.pre_grasp_posture.header.stamp = rospy.Time(0)
			newGrasp.grasp_posture.header.stamp = rospy.Time(0)
			newGrasp.grasp_pose.header.frame_id = 'world'
			newGrasp.grasp_pose.pose.position.x += pose.pose.position.x
			newGrasp.grasp_pose.pose.position.y += pose.pose.position.y
			newGrasp.grasp_pose.pose.position.z += pose.pose.position.z
			newGrasp.grasp_quality = 1.0
			correctedGrasps.append(newGrasp)

		return correctedGrasps

	def publishMarkers(self, grasps, object_name):
		for grasp in grasps:
			marker = self.getMarker(grasp, object_name)
			self.markers_publisher.publish(marker)
		

	def getMarker(self, grasp, object_name):
		marker = Marker()
		marker.id = int(grasp.id)
		marker.header = grasp.grasp_pose.header
		marker.header.frame_id = grasp.grasp_pose.header.frame_id
		marker.pose = grasp.grasp_pose.pose
		marker.ns = object_name + "_grasp_"
		marker.lifetime.secs = 1
		marker.action = 0
		marker.color.r = 1
		marker.color.g = 1
		marker.color.b = 1
		marker.color.a = 1
		marker.scale.x = .1
		marker.scale.y = .1
		marker.scale.z = .1
		return marker

	def solveIK(self, pose, limb):
		ns = "/ExternalTools/" + limb + "/PositionKinematicsNode/IKService"
		if self.iksvc == None:
			rospy.wait_for_service(ns, 5.0)
			self.iksvc = rospy.ServiceProxy(ns, SolvePositionIK)
		ikreq = SolvePositionIKRequest()
		hdr = Header(stamp=rospy.Time.now(), frame_id='base')
		goalPose = PoseStamped(header=hdr, pose=pose)

		ikreq.pose_stamp.append(goalPose)
		try:
			rospy.loginfo(ikreq)
			resp = self.iksvc(ikreq)
		except (rospy.ServiceException, rospy.ROSException), e:
		    rospy.logerr("Service call failed: %s" % (e,))
		    return 1
		if (resp.isValid[0]):
		    limb_joints = dict(zip(resp.joints[0].name, resp.joints[0].position))
		    return limb_joints;
		else:
		    rospy.logwarn("INVALID POSE - No Valid Joint Solution Found.")
		    return None



	def go(self, args):
		#moveit_commander.roscpp_initialize(args)
		#left_arm = baxter_interface.limb.Limb("left")
		#left_arm.move_to_neutral()
		self.moveToNeutral()
		self.scene.remove_world_object("coconut")
		self.scene.remove_world_object("table")
		self.addTable()
		rospy.sleep(5.0)
		pose = self.attachObject("coconut")

		while not self.pick(pose, "coconut"):
			pass
		place_poses = self.getValidPlacePoses()
		place_result = False
		for place_pose in place_poses:
			if self.place("coconut", place_pose):
				break

if __name__=='__main__':
	rospy.init_node("place")
	place = Place()
	place.go(sys.argv)