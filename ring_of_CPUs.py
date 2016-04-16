from mpi4py import MPI

import numpy as np

import communication_helpers as ch

class RingOfCPUs(object):
	def __init__(self, sim_content, N_pieces_per_transfer=1, single_CPU_mode = False, comm=None):
		
		self.sim_content = sim_content
		self.N_turns = sim_content.N_turns
		self.N_pieces_per_transfer = N_pieces_per_transfer
		
		self.sim_content.ring_of_CPUs = self
		
		# check if user is forcing simulation mode
		if single_CPU_mode:
			print '\nSingle_CPU_forced_by_user!\n'
			self.comm = SingleCoreComminicator()
		elif comm is not None:
			self.comm = comm
		else:
			from mpi4py import MPI
			self.comm = MPI.COMM_WORLD
			
		#check if there is only one node
		if self.comm.Get_size()==1:
			#in case it is forced by user it will be rebound but there is no harm in that
			self.comm = SingleCoreComminicator()
			
		# get info on the grid
		self.N_nodes = self.comm.Get_size()
		self.N_wkrs = self.N_nodes-1
		self.master_id = self.N_nodes-1
		self.myid = self.comm.Get_rank()
		self.I_am_a_worker = self.myid!=self.master_id
		self.I_am_the_master = not(self.I_am_a_worker)

		# allocate buffers for communication
		self.N_buffer_float_size = 1000000
		self.buf_float = np.array(self.N_buffer_float_size*[0.])
		self.N_buffer_int_size = 100
		self.buf_int = np.array(self.N_buffer_int_size*[0])

		self.sim_content.init_all()

		self.comm.Barrier() # only for stdoutp

		if self.I_am_the_master:
			self.pieces_to_be_treated = self.sim_content.init_master()
			self.N_pieces = len(self.pieces_to_be_treated)
			self.pieces_treated = []
			self.i_turn = 0
			self.piece_to_send = None
		elif self.I_am_a_worker:
			self.sim_content.init_worker()
			# Identify CPUs on my left and my right
			if self.myid==0:
				self.left = self.master_id
			else:
				self.left = self.myid-1
			self.right = self.myid+1
			
		self.comm.Barrier() # wait that all are done with the init

	def run(self):
		if self.I_am_the_master:
			import time
			t_last_turn = time.mktime(time.localtime())
			while True: #(it will be stopped with a break)
				orders_from_master = []
				list_of_buffers_to_send = []
				
				for _ in xrange(self.N_pieces_per_transfer):
					# pop a piece
					try:
						piece_to_send = self.pieces_to_be_treated.pop() 	# pop starts for the last slices 
																	# (it is what we want, for the HEADTAIL 
																	# slice order convention, z = -beta*c*t)
					except IndexError:
						piece_to_send = None
						
					list_of_buffers_to_send.append(self.sim_content.piece_to_buffer(piece_to_send))

				# send it to the first element of the ring and receive from the last
				sendbuf = ch.combine_float_buffers(list_of_buffers_to_send)
				if len(sendbuf)	> self.N_buffer_float_size:
					raise ValueError('Float buffer is too small!')
				self.comm.Sendrecv(sendbuf, dest=0, sendtag=0, 
							recvbuf=self.buf_float, source=self.master_id-1, recvtag=self.myid)
				list_received_pieces = map(self.sim_content.buffer_to_piece, ch.split_float_buffers(self.buf_float))
				
				# treat received pieces				
				for piece_received in list_received_pieces:
					if piece_received is not None:
						self.sim_content.treat_piece(piece_received)
						self.pieces_treated.append(piece_received)	

				# end of turn
				if len(self.pieces_treated)==self.N_pieces:	

					self.pieces_treated = self.pieces_treated[::-1] #restore the original order

					# perform global operations and reslice
					orders_to_pass, new_pieces_to_be_treated = \
						self.sim_content.finalize_turn_on_master(self.pieces_treated)
					orders_from_master += orders_to_pass
					
					t_now = time.mktime(time.localtime())
					print 'Turn %d, %d s'%(self.i_turn,t_now-t_last_turn) 
					t_last_turn = t_now

					# prepare next turn
					self.pieces_to_be_treated = new_pieces_to_be_treated
					self.N_pieces = len(self.pieces_to_be_treated)
					self.pieces_treated = []			
					self.i_turn+=1

					# check if stop is needed
					if self.i_turn == self.N_turns: orders_from_master.append('stop')		
								
				# send orders
				buforders = ch.list_of_strings_2_buffer(orders_from_master)
				if len(buforders) > self.N_buffer_int_size:
					raise ValueError('Int buffer is too small!')
				self.comm.Bcast(buforders, self.master_id)

				#execute orders from master (the master executes its own orders :D)
				self.sim_content.execute_orders_from_master(orders_from_master)

				# check if simulation has to be ended
				if 'stop' in orders_from_master:
					break

			# finalize simulation (savings etc.)	
			self.sim_content.finalize_simulation()				

		elif self.I_am_a_worker:
			# initialization 
			list_of_buffers_to_send = [self.sim_content.piece_to_buffer(None)]
			
			while True:

				sendbuf = ch.combine_float_buffers(list_of_buffers_to_send)
				if len(sendbuf)	> self.N_buffer_float_size:
					raise ValueError('Float buffer is too small!')
				self.comm.Sendrecv(sendbuf, dest=self.right, sendtag=self.right, 
							recvbuf=self.buf_float, source=self.left, recvtag=self.myid)
				list_received_pieces = map(self.sim_content.buffer_to_piece, ch.split_float_buffers(self.buf_float))

				# treat received piece
				for piece_received in list_received_pieces:
					if piece_received is not None:
						self.sim_content.treat_piece(piece_received) #the elements of the list are being mutated

				# prepare for next iteration
				list_of_buffers_to_send = map(self.sim_content.piece_to_buffer, list_received_pieces)

				# receive orders from the master
				self.comm.Bcast(self.buf_int, self.master_id)
				orders_from_master = ch.buffer_2_list_of_strings(self.buf_int)

				#execute orders from master
				self.sim_content.execute_orders_from_master(orders_from_master)

				# check if simulation has to be ended
				if 'stop' in orders_from_master:
					break

# # usage
# from Simulation import Simulation
# sim_content = Simulation()

# myring = RingOfCPUs(sim_content, N_turns)

# myring.run()

class SingleCoreComminicator(object):
	def __init__(self):
		print '\n\n\n'
		print '****************************************'	
		print '*** Using single core MPI simulator! ***'	
		print '****************************************'
		print '\n\n\n'
		
	def Get_size(self):
		return 1
	
	def Get_rank(self):
		return 0
	
	def Barrier(self):
		pass

	def Sendrecv(self, sendbuf, dest, sendtag, recvbuf, source, recvtag):
		if dest!=0 or sendtag!=0 or source!=-1 or recvtag!=0:
			raise ValueError('Input of Sendrecv not compatible with single core operation!!!')
		recvbuf[:len(sendbuf)]=sendbuf
		
	def Bcast(self, buf, root=0):
		if root!=0:
			raise ValueError('Input of Bcast not compatible with single core operation!!!')
		#Does not really have to do anything

# # usage
# from Simulation import Simulation
# sim_content = Simulation()

# myring = RingOfCPUs(sim_content, N_turns)

# myring.run()
