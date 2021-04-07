__all__ = [
	"EBCOTCodec"
]

from copy import deepcopy
from multiprocessing import Pool
import numpy as np

from fpeg.base import Codec
from fpeg.config import read_config
from fpeg.funcs import parse_marker, cat_arrays_2d

config = read_config()

D = config.get("jpeg2000", "D")
G = config.get("jpeg2000", "G")
QCD = config.get("jpeg2000", "QCD")
mq_table = config.get("jpeg2000", "mq_table")

min_task_number = config.get("accelerate", "codec_min_task_number")
max_pool_size = config.get("accelerate", "codec_max_pool_size")


class EBCOTCodec(Codec):
	"""
	EBCOT codec
	"""

	def __init__(self,
							 name="EBCOT codec",
							 mode="encode",
							 D=D,
							 G=G,
							 QCD=QCD,
							 accelerated=False
							 ):
		"""
		Init and set attributes of a canonical EBCOT codec.

		Explicit Attributes
		-------------------
		name: str, optional
			Name of the codec.
		mode: str, optional
			Mode of the codec, must in ["encode", "decode"].
		G: integer, optional
			a parameter for calculate Kmax
		D: integer, optional
			Depth of graphic.
		epsilon_b:integer, must
			a parameter for calculate Kmax
		accelerated: bool, optional
			Whether the process would be accelerated by subprocess pool.

		"""
		super().__init__()

		self.name = name
		self.mode = mode
		# based on equation 10.22 in jpeg2000
		self.D = D
		self.G = G
		self.QCD = QCD
		self.accelerated = accelerated

		self.epsilon_b, _ = parse_marker(self.QCD)
		self.Kmax = max(0, self.G + self.epsilon_b - 1)
		self.min_task_number = min_task_number
		self.max_pool_size = max_pool_size

	def encode(self, X, **params):
		self.logs[-1] += self.formatter.message("Trying to encode received data.")
		try:
			self.epsilon_b = params["epsilon_b"]
		except KeyError:
			pass


		if self.accelerated:
			self.logs[-1] += self.formatter.message("Using multiprocess pool to accelerate EBCOT encoding.")
			inputs = [[x, self.D] for x in X]
			with Pool(min(self.task_number, self.max_pool_size)) as p:
				bitcodes = p.starmap(_EBCOT_encode, inputs)
		else:
			bitcodes = [_EBCOT_encode(x, self.D) for x in X]

		return bitcodes

	def decode(self, bitcodes, **params):
		self.logs[-1] += self.formatter.message("Trying to decode received data.")

		if self.accelerated:
			inputs = [[bitcode, self.D] for bitcode in bitcodes]
			with Pool(min(self.task_number, self.max_pool_size)) as p:
				X = p.starmap(_tile_decode, inputs)
		else:
			X = [_tile_decode(bitcode, self.D) for bitcode in bitcodes]

		return X


def _EBCOT_encode(tile, D):
	"""
	EBCOT encode and decode part
	encode part:
	| _EBCOT_encode
		| _tile_encode
			| _band_encode
				| _embeddedBlockEncoder _MQencode
					 | 三个通道过程

	｜ _EBCOT_decode
		| _tile_decode
			| _band_decode
				| _block_decode
					| three decode passes and MQdecode
						|signdecode and runlengthdecode
	"""

	bitcode = list(_tile_encode(tile, D))

	# with open('test.bin', 'wb') as f:
	#   f.write(struct.pack(str(l)+'i', *bitcode))
	# streamonly = [int(i) for i in streamonly]
	# l = len(streamonly)
	# with open('streamonly.bin', 'wb') as f:
	#   f.write(struct.pack(str(l)+'i', *streamonly))

	return bitcode


def _MQencode(CX, D):
	"""
	MQ encode and decode part
	|MQencode
		|transferbyte
			|putbyte
	|MQdecode
		|fill_lsp

	"""
	PETTable, CXTable = deepcopy(mq_table)
	encoder = EBCOTparam()
	for i in range(len(D)):
		symbol = D[i][0]
		cxLabel = CX[i][0]
		expectedSymbol = CXTable[cxLabel][1]
		p = PETTable[CXTable[cxLabel][0]][3]  # PETTable [CXTable[cxLabel][0]]---[3]
		encoder.A = encoder.A - p
		if encoder.A < p:
			# Conditional exchange of MPS and LPS
			expectedSymbol = 1 - expectedSymbol
		if symbol == expectedSymbol:
			# assign MPS the upper sub-interval
			encoder.C = encoder.C + np.uint32(p)
		else:
			# assign LPS the lower sub-interval
			encoder.A = np.uint32(p)
		if encoder.A < 32768:
			if symbol == CXTable[cxLabel][1]:
				CXTable[cxLabel][0] = PETTable[CXTable[cxLabel][0]][0]
			else:
				CXTable[cxLabel][1] = CXTable[cxLabel][1] ^ PETTable[CXTable[cxLabel][0]][2]
				CXTable[cxLabel][0] = PETTable[CXTable[cxLabel][0]][1]
			while encoder.A < 32768:
				encoder.A = 2 * encoder.A
				encoder.C = 2 * encoder.C
				encoder.t = encoder.t - 1
				if encoder.t == 0:
					encoder = _transferbyte(encoder)
	encoder = _encode_end(encoder)
	return encoder


def _transferbyte(encoder):
	CPartialMask = np.uint32(133693440)  # 00000111111110000000000000000000
	CPartialCmp = np.uint32(4161273855)  # 11111000000001111111111111111111
	CMsbsMask = np.uint32(267386880)  # 27-20msbs标志位  00001111111100000000000000000000
	CMsbsCmp = np.uint32(4027580415)  # CMsbs的补码      11110000000011111111111111111111
	CCarryMask = np.uint32(2 ** 27)  # 取进位              00001000000000000000000000000000
	if encoder.T == 255:
		# 不能将任何进位传给T，需要位填充
		encoder = _putbyte(encoder)
		encoder.T = np.uint8((encoder.C & CMsbsMask) >> 20)  # 27-20位
		encoder.C = encoder.C & CMsbsCmp  #
		encoder.t = 7
	else:
		# 从C将任何进位传到T
		encoder.T = encoder.T + np.uint8((encoder.C & CCarryMask) >> 27)
		encoder.C = encoder.C ^ CCarryMask
		encoder = _putbyte(encoder)
		if encoder.T == 255:
			encoder.T = np.uint8((encoder.C & CMsbsMask) >> 20)
			encoder.C = encoder.C & CMsbsCmp
			encoder.t = 7
		else:
			encoder.T = np.uint8((encoder.C & CPartialMask) >> 19)
			encoder.C = encoder.C & CPartialCmp
			encoder.t = 8
	return encoder


def _putbyte(encoder):
	# 将T中的内容写入字节缓存
	if encoder.L >= 0:
		encoder.stream = np.append(encoder.stream, encoder.T)
	encoder.L = encoder.L + 1
	return encoder


def _MQ_decode(stream, CX):
	PETTable, CXTable = deepcopy(mq_table)
	# MQ decode initializtion
	encoder = EBCOTparam()
	encoder.A = np.uint16(0)
	encoder.C = np.uint32(0)
	encoder.t = np.uint8(0)
	encoder.T = np.uint8(0)
	encoder.L = np.int32(0)
	encoder.stream = stream
	encoder = _fill_lsb(encoder)
	encoder.C = encoder.C << encoder.t
	encoder = _fill_lsb(encoder)
	encoder.C = encoder.C << 7
	encoder.t = encoder.t - 7
	encoder.A = np.uint16(2 ** 15)

	# MQ decode procedure
	CActiveMask = np.uint32(16776960)  # 00000000111111111111111100000000
	CActiveCmp = np.uint32(4278190335)  # 11111111000000000000000011111111
	decodeD = []
	for i in range(len(CX)):
		cxLabel = CX[i][0]
		expectedSymbol = CXTable[cxLabel][1]
		p = PETTable[CXTable[cxLabel][0]][3]
		encoder.A = encoder.A - np.uint16(p)
		if encoder.A < np.uint16(p):
			expectedSymbol = 1 - expectedSymbol
		if ((encoder.C & CActiveMask) >> 8) < p:
			symbol = 1 - expectedSymbol
			encoder.A = np.uint16(p)
		else:
			symbol = expectedSymbol
			temp = ((encoder.C & CActiveMask) >> 8) - np.uint32(p)
			encoder.C = encoder.C & CActiveCmp
			encoder.C = encoder.C + np.uint32((np.uint32(temp << 8)) & CActiveMask)
		if encoder.A < 2 ** 15:
			if symbol == CXTable[cxLabel][1]:
				CXTable[cxLabel][0] = PETTable[CXTable[cxLabel][0]][0]
			else:
				CXTable[cxLabel][1] = CXTable[cxLabel][1] ^ PETTable[CXTable[cxLabel][0]][2]
				CXTable[cxLabel][0] = PETTable[CXTable[cxLabel][0]][1]
			while encoder.A < 2 ** 15:
				if encoder.t == 0:
					encoder = _fill_lsb(encoder)
				encoder.A = 2 * encoder.A
				encoder.C = 2 * encoder.C
				encoder.t = encoder.t - 1
		# print(i, symbol)
		decodeD.append([symbol])
	return decodeD


def _fill_lsb(encoder):
	encoder.t = 8
	if encoder.L == len(encoder.stream) or \
			(encoder.T == 255 and encoder.stream[encoder.L] > 143):
		encoder.C = encoder.C + 255
	else:
		if encoder.T == 255:
			encoder.t = 7
		encoder.T = encoder.stream[encoder.L]
		encoder.L = encoder.L + 1
		encoder.C = encoder.C + np.uint32((encoder.T) << (8 - encoder.t))
	return encoder


def _tile_encode(tile, D, h=64, w=64):
	_depthOfDwt = D

	tile_cA = tile[0][:, :, 0]
	newBit, newStream = _band_encode(tile_cA, 'LL', h, w)
	bitcode = newBit
	streamOnly = newStream
	for i in range(1, _depthOfDwt+1):
		newBit, newStream = _band_encode(tile[i][0][:, :, 0], 'LH', h, w)
		bitcode = np.hstack((bitcode, newBit))
		streamOnly = np.hstack((streamOnly, newStream))
		newBit, newStream = _band_encode(tile[i][1][:, :, 0], 'HL', h, w)
		bitcode = np.hstack((bitcode, newBit))
		streamOnly = np.hstack((streamOnly, newStream))
		newBit, newStream = _band_encode(tile[i][2][:, :, 0], 'HH', h, w)
		bitcode = np.hstack((bitcode, newBit))
		streamOnly = np.hstack((streamOnly, newStream))

	tile_cA = tile[0][:, :, 1]
	newBit, newStream = _band_encode(tile_cA, 'LL', h, w)
	bitcode = np.hstack((bitcode, newBit))
	streamOnly = np.hstack((streamOnly, newStream))
	for i in range(1, _depthOfDwt+1):
		newBit, newStream = _band_encode(tile[i][0][:, :, 1], 'LH', h, w)
		bitcode = np.hstack((bitcode, newBit))
		streamOnly = np.hstack((streamOnly, newStream))
		newBit, newStream = _band_encode(tile[i][1][:, :, 1], 'HL', h, w)
		bitcode = np.hstack((bitcode, newBit))
		streamOnly = np.hstack((streamOnly, newStream))
		newBit, newStream = _band_encode(tile[i][2][:, :, 1], 'HH', h, w)
		bitcode = np.hstack((bitcode, newBit))
		streamOnly = np.hstack((streamOnly, newStream))

	tile_cA = tile[0][:, :, 2]
	newBit, newStream = _band_encode(tile_cA, 'LL', h, w)
	bitcode = np.hstack((bitcode, newBit))
	streamOnly = np.hstack((streamOnly, newStream))
	for i in range(1, _depthOfDwt+1):
		newBit, newStream = _band_encode(tile[i][0][:, :, 2], 'LH', h, w)
		bitcode = np.hstack((bitcode, newBit))
		streamOnly = np.hstack((streamOnly, newStream))
		newBit, newStream = _band_encode(tile[i][1][:, :, 2], 'HL', h, w)
		bitcode = np.hstack((bitcode, newBit))
		streamOnly = np.hstack((streamOnly, newStream))
		newBit, newStream = _band_encode(tile[i][2][:, :, 2], 'HH', h, w)
		bitcode = np.hstack((bitcode, newBit))
		streamOnly = np.hstack((streamOnly, newStream))
	bitcode = np.hstack((bitcode, [2051]))
	print("streamonly"+str(len(streamOnly)))
	print("bitcode"+str(len(bitcode)))
	return bitcode


def _band_encode(tile, bandMark, h=64, w=64, num=8):
	# 码流：[h, w, CX1, 2048, stream1, 2048, ..., CXn, streamn, 2048, 2049,CXn+1, streamn+1, 2048, ...,2050]
	h_cA, w_cA = np.shape(tile)
	h_left_over = h_cA % h
	w_left_over = w_cA % w
	cA_extend = np.pad(tile, ((0, h - h_left_over), (0, w - w_left_over)), 'constant')
	bitcode = [h_cA, w_cA]
	streamOnly = []
	for i in range(0, h_cA, h):
		for j in range(0, w_cA, w):
			codeBlock = cA_extend[i:i + h, j:j + w]
			CX, D, bitplanelength= _embeddedBlockEncoder(codeBlock, bandMark, h, w, num)
			encoder = _MQencode(CX, D)
			bitcode = np.hstack((bitcode, CX.flatten(), [2048], encoder.stream, [2048], bitplanelength,[2048]))
			streamOnly = np.hstack((streamOnly, encoder.stream))
		bitcode = np.hstack((bitcode, [2049]))
	bitcode = np.hstack((bitcode, [2050]))
	return (bitcode, streamOnly)


def _embeddedBlockEncoder(codeBlock, bandMark, h=64, w=64, num=8):
	S1 = np.zeros((h, w))
	S2 = np.zeros((h, w))
	S3 = np.zeros((h, w))
	MaxInCodeBlock = len(bin(int(np.max(abs(codeBlock)))))-2
	signs = (- np.sign(codeBlock) + 1) // 2  # positive: 0, negative: 1
	bitPlane = np.zeros((h,w,MaxInCodeBlock),dtype=np.uint8)
	for i in range(h):
		for j in range(w):
			# number = bin(np.abs(codeBlock[i][j], dtype=np.int64))[2:]
			number = bin(np.abs(codeBlock[i][j]))[2:]
			temp = [0] * (MaxInCodeBlock-len(number)) + [int(num) for num in number]
			bitPlane[i][j] = np.array(temp)
	bitPlane = np.transpose(bitPlane, (2, 0, 1))
	# For Test
	"""
	signs = np.zeros((8,8))
	bitPlane = np.zeros((2,8,8))
	bitPlane[0][1][1] = 1
	bitPlane[0][4][4] = 1
	bitPlane[1][0][2] = 1
	bitPlane[1][1] = np.array([0,1,0,0,1,1,0,0])
	bitPlane[1][2][2] = 1
	bitPlane[1][3][3] = 1
	bitPlane[1][4][5] = 1
	bitPlane[1][5] = np.array([0,0,0,0,1,1,0,1])
	bitPlane[1][6][6] = 1
	"""
	sizeofCXandD = h*w*MaxInCodeBlock *5
	CX = np.zeros((sizeofCXandD, 1), dtype=np.uint32)
	D = np.zeros((sizeofCXandD, 1), dtype=np.uint32)
	pointer = 0
	for i in range(MaxInCodeBlock ):
		######
		# three function need rename
		D, CX, S1, S3, pointer = _SignifiancePropagationPass(D, CX, S1, S3, pointer, bitPlane[i], bandMark, signs, w, h)
		D, CX, S2, pointer = _MagnitudeRefinementPass(D, CX, S1, S2, S3, pointer, bitPlane[i], w, h)
		D, CX, pointer, S1 = _CLeanUpPass(D, CX, S1, S3, pointer, bitPlane[i], bandMark, signs, w, h)
		S3 = np.zeros((h, w))
	CX_final = CX[0:pointer]
	D_final = D[0:pointer]
	return CX_final, D_final, MaxInCodeBlock


# three encode pass start here
# in the sequence of significancePass,magnitudepass,_cleanuppass.

def _SignifiancePropagationPass(D, CX, S1, S3, pointer, plane, bandMark, signs, w=64, h=64):
	# input S1:  list of significance, size 64*64
	# input CX: the list of context
	# plane: the value of bits at this plane
	# bandMark: LL, HL, HH, or LH
	# pointer: the pointer of the CX
	# S3: denote that the element has been coded
	# output: D, CX, S1, S3, pointer
	S1extend = np.pad(S1, ((1, 1), (1, 1)), 'constant')
	rounds = h // 4
	for i in range(rounds):
		for col in range(w):
			for ii in range(4):
				row = 4 * i + ii
				if S1[row][col] != 0:
					continue  # is significant
				temp = S1extend[row][col] + S1extend[row + 1][col] + S1extend[row + 2][col] + S1extend[row][col + 1] + \
							 S1extend[row + 2][col + 1] + S1extend[row][col + 2] + S1extend[row + 1][col + 2] + S1extend[row + 2][
								 col + 2]
				if temp == 0:
					continue  # is insignificant
				tempCx = _ZeroCoding(S1extend[row:row + 3, col:col + 3], bandMark)
				D[pointer][0] = plane[row][col]
				CX[pointer][0] = tempCx
				pointer = pointer + 1
				S3[row][col] = 1  # mark that plane[row][col] has been coded
				if plane[row][col] == 1:  # _signcoding
					signComp, tempCx = _SignCoding(S1extend[row:row + 3, col:col + 3], signs[row][col])
					D[pointer][0] = signComp
					CX[pointer][0] = tempCx
					pointer = pointer + 1
					S1[row][col] = 1  # mark as significant
					S1extend = np.pad(S1, ((1, 1), (1, 1)), 'constant')
	return D, CX, S1, S3, pointer


def _MagnitudeRefinementPass(D, CX, S1, S2, S3, pointer, plane, w=64, h=64):
	S1extend = np.pad(S1, ((1, 1), (1, 1)), 'constant')
	rounds = h // 4
	for i in range(rounds):
		for col in range(w):
			for ii in range(4):
				row = 4 * i + ii
				if S1[row][col] != 1 or S3[row][col] != 0:
					continue
				tempCx = _MagnitudeRefinementCoding(S1extend[row:row + 3, col:col + 3], S2[row][col])
				S2[row][col] = 1  # Mark that the element has been refined
				D[pointer][0] = plane[row][col]
				CX[pointer][0] = tempCx
				pointer = pointer + 1
	return D, CX, S2, pointer


def _CLeanUpPass(D, CX, S1, S3, pointer, plane, bandMark, signs, w=64, h=64):
	S1extend = np.pad(S1, ((1, 1), (1, 1)), 'constant')
	rounds = h // 4
	for i in range(rounds):
		for col in range(w):
			ii = 0
			row = 4 * i
			tempSum = np.sum(S1extend[row:row + 6, col:col + 3]) + np.sum(S3[row:row + 4, col])
			# 整一列未被编码，都为非重要，且领域非重要
			if tempSum == 0:
				ii, tempD, tempCx = _RunLengthCoding(plane[row:row + 4, col])
				if len(tempD) == 1:
					D[pointer] = tempD
					CX[pointer] = tempCx
					pointer = pointer + 1
				else:
					D[pointer], D[pointer + 1], D[pointer + 2] = tempD[0], tempD[1], tempD[2]
					CX[pointer], CX[pointer + 1], CX[pointer + 2] = tempCx[0], tempCx[1], tempCx[2]
					pointer = pointer + 3
					# sign coding
					row = i * 4 + ii - 1
					signComp, tempCx = _SignCoding(S1extend[row:row + 3, col:col + 3], signs[row][col])
					D[pointer] = signComp
					CX[pointer] = tempCx
					pointer = pointer + 1
					S1[row][col] = 1
					S1extend = np.pad(S1, ((1, 1), (1, 1)), 'constant')
			while ii < 4:
				row = i * 4 + ii
				ii = ii + 1
				if S1[row][col] != 0 or S3[row][col] != 0:
					continue
				tempCx = _ZeroCoding(S1extend[row:row + 3, col:col + 3], bandMark)
				D[pointer] = plane[row][col]
				CX[pointer] = tempCx
				pointer = pointer + 1
				if plane[row][col] == 1:  # _signcoding
					signComp, tempCx = _SignCoding(S1extend[row:row + 3, col:col + 3], signs[row][col])
					D[pointer][0] = signComp
					CX[pointer][0] = tempCx
					pointer = pointer + 1
					S1[row][col] = 1  # mark as significant
					S1extend = np.pad(S1, ((1, 1), (1, 1)), 'constant')
	return D, CX, pointer, S1


# here is some function used by three passes

def _SignCoding(neighbourS1, sign):
	# input neighbourS1: size 3*3, matrix of significance
	# input sign
	# output: signComp,(equal: 0, not equal: 1) context
	if len(neighbourS1) == 3 and len(neighbourS1[0]) == 3:
		hstr = str(int(neighbourS1[1][0])) + str(int(neighbourS1[1][2]))
		vstr = str(int(neighbourS1[0][1])) + str(int(neighbourS1[2][1]))
		dict = {
			'00': 0, '1-1': 0, '-11': 0, '01': 1, '10': 1, '11': 1,
			'0-1': -1, '-10': -1, '-1-1': -1
		}
		h = dict[hstr]
		v = dict[vstr]
		hAndv = str(h) + str(v)
		hv2Sign = {
			'11': 0, '10': 0, '1-1': 0, '01': 0, '00': 0,
			'0-1': 1, '-11': 1, '-10': 1, '-1-1': 1
		}
		hv2Context = {
			'11': 13, '10': 12, '1-1': 11, '01': 10, '00': 9,
			'0-1': 10, '-11': 11, '-10': 12, '-1-1': 13
		}
		signPredict = hv2Sign[hAndv]
		context = hv2Context[hAndv]
		signComp = int(sign) ^ signPredict
	else:
		# self.logs[-1] += self.formatter.warning("_SignCoding: Size of neighbourS1 not valid")
		signComp = -1
		context = -1
		"""
		try:
			raise ValidationError('_SignCoding: Size of neighbourS1 not valid')
		except ValidationError as e:
			print(e.args)
			signComp = -1
			context = -1
		"""
	return signComp, context


def _ZeroCoding(neighbourS1, bandMark):
	# input neighbourS1: size 3*3, matrix of significance
	# input s2: whether it is the first time for Magnitude Refinement Coding
	# output: context
	if len(neighbourS1) == 3 and len(neighbourS1[0]) == 3:
		h = neighbourS1[1][0] + neighbourS1[1][2]
		v = neighbourS1[0][1] + neighbourS1[2][1]
		d = neighbourS1[0][0] + neighbourS1[0][2] + neighbourS1[2][0] + neighbourS1[2][2]
		if bandMark == 'LL' or bandMark == 'LH':
			if h == 2:
				cx = 8
			elif h == 1 and v >= 1:
				cx = 7
			elif h == 1 and v == 0 and d >= 1:
				cx = 6
			elif h == 1 and v == 0 and d == 0:
				cx = 5
			elif h == 0 and v == 2:
				cx = 4
			elif h == 0 and v == 1:
				cx = 3
			elif h == 0 and v == 0 and d >= 2:
				cx = 2
			elif h == 0 and v == 0 and d == 1:
				cx = 1
			else:
				cx = 0
		elif bandMark == 'HL':
			if v == 2:
				cx = 8
			elif v == 1 and h >= 1:
				cx = 7
			elif v == 1 and h == 0 and d >= 1:
				cx = 6
			elif v == 1 and h == 0 and d == 0:
				cx = 5
			elif v == 0 and h == 2:
				cx = 4
			elif v == 0 and h == 1:
				cx = 3
			elif v == 0 and h == 0 and d >= 2:
				cx = 2
			elif v == 0 and h == 0 and d == 1:
				cx = 1
			else:
				cx = 0
		elif bandMark == 'HH':
			hPlusv = h + v
			if d >= 3:
				cx = 8
			elif d == 2 and hPlusv >= 1:
				cx = 7
			elif d == 2 and hPlusv == 0:
				cx = 6
			elif d == 1 and hPlusv >= 2:
				cx = 5
			elif d == 1 and hPlusv == 1:
				cx = 4
			elif d == 1 and hPlusv == 0:
				cx = 3
			elif d == 0 and hPlusv >= 2:
				cx = 2
			elif d == 0 and hPlusv == 1:
				cx = 1
			else:
				cx = 0
		else:
			# self.logs[-1] += self.formatter.warning('_ZeroCoding: bandMark not valid')
			cx = -1
			"""
			try:
				raise ValidationError('_ZeroCoding: bandMark not valid')
			except ValidationError as e:
				print(e.args)
				cx = -1
			"""
	else:
		# self.logs[-1] += self.formatter.warning('_ZeroCoding: Size of neighbourS1 not valid')
		cx = -1
		"""
		try:
			raise ValidationError('_ZeroCoding: Size of neighbourS1 not valid')
		except ValidationError as e:
			print(e.args)
			cx = -1
		"""
	return cx


def _RunLengthCoding(listS1):
	# input listS1: size 1*4, list of significance
	# output n: number of elements encoded
	# output d: 0 means the _RunLengthCoding does not end.
	# [1, x, x] means the _RunLengthCoding ends and the position is indicated.
	# output cx: context
	if listS1.__len__() == 4:
		if listS1[0] == 0 and listS1[1] == 0 and listS1[2] == 0 and listS1[3] == 0:
			n = 4
			d = [0]
			cx = [17]
		elif listS1[0] == 1:
			n = 1
			d = [1, 0, 0]
			cx = [17, 18, 18]
		elif listS1[0] == 0 and listS1[1] == 1:
			n = 2
			d = [1, 0, 1]
			cx = [17, 18, 18]
		elif listS1[0] == 0 and listS1[1] == 0 and listS1[2] == 1:
			n = 3
			d = [1, 1, 0]
			cx = [17, 18, 18]
		elif listS1[0] == 0 and listS1[1] == 0 and listS1[2] == 0 and listS1[3] == 1:
			n = 4
			d = [1, 1, 1]
			cx = [17, 18, 18]
		else:
			# self.logs[-1] += self.formatter.warning('_RunLengthCoding: listS1 not valid')
			n, d, cx = 0, -1, -1
			"""
			try:
				raise ValidationError('_RunLengthCoding: listS1 not valid')
			except ValidationError as e:
				print(e.args)
				n, d, cx = 0, -1, -1
			"""
	else:
		# self.logs[-1] += self.formatter.warning('_RunLengthCoding: length of listS1 not valid')
		n, d, cx = 0, -1, -1
		"""
		try:
			raise ValidationError('_RunLengthCoding: length of listS1 not valid')
		except ValidationError as e:
			print(e.args)
			n, d, cx = 0, -1, -1
		"""
	return n, d, cx


def _MagnitudeRefinementCoding(neighbourS1, s2):
	# input neighbourS1: size 3*3, matrix of significance
	# input s2: whether it is the first time for Magnitude Refinement Coding
	# output: context
	if len(neighbourS1) == 3 and len(neighbourS1[0]) == 3:
		temp = np.sum(neighbourS1) - neighbourS1[1][1]
		if s2 == 1:
			cx = 16
		elif s2 == 0 and temp >= 1:
			cx = 15
		else:
			cx = 14
	else:
		# self.logs[-1] += self.formatter.warning('_MagnitudeRefinementCoding: Size of neighbourS1 not valid')
		cx = -1
		"""
		try:
			raise ValidationError('_MagnitudeRefinementCoding: Size of neighbourS1 not valid')
		except ValidationError as e:
			print(e.args)
			cx = -1
		"""
	return cx


def _encode_end(encoder):
	nbits = 27 - 15 - encoder.t
	encoder.C = encoder.C * np.uint32(2 ** encoder.t)
	while nbits > 0:
		encoder = _transferbyte(encoder)
		nbits = nbits - encoder.t
		encoder.C = encoder.C * np.uint32(2 ** encoder.t)
	encoder = _transferbyte(encoder)

	return encoder

def _tile_decode(codestream, D):
	_depthOfDWT = D
	temp = []
	for i in range(0, 9 * _depthOfDWT + 3):
		_index = codestream.index(2050)
		deStream = codestream[0:_index + 1]
		temp.append(_band_decode(deStream))
		codestream = codestream[_index + 1:]
					
	start1 = _depthOfDWT*3+1
	start2 = _depthOfDWT*6+2
	tile = [cat_arrays_2d([temp[0],
												temp[start1],
												temp[start2]])]
	
	for i in range(_depthOfDWT):
		
		tile.append((cat_arrays_2d([temp[3 * i + 1],
															 temp[3 * i + start1+1],
															 temp[3 * i + start2+1]]),
								 cat_arrays_2d([temp[3 * i + 2],
															 temp[3 * i + start1+2],
															 temp[3 * i + start2+2]]),
								 cat_arrays_2d([temp[3 * i + 3],
															 temp[3 * i + start1+3],
															 temp[3 * i + start2+3]])))
	
	return tile


def _band_decode(codestream, h=64, w=64, num=32):
	h_cA = codestream[0]
	w_cA = codestream[1]
	codestream = codestream[2:]
	h_num = h_cA // h + 1
	w_num = w_cA // w + 1
	band_extend = np.zeros((h_num * h, w_num * w))
	for i in range(0, h_num):
		for j in range(0, w_num):
			_index = codestream.index(2048)
			deCX = codestream[0:_index]
			deCX = np.resize(deCX, (_index + 1, 1))
			codestream = codestream[_index + 1:]
			_index = codestream.index(2048)
			deStream = codestream[0:_index]
			codestream = codestream[_index + 1:]
			num = codestream[0]
			codestream = codestream[2:]
			decodeD = _MQ_decode(deStream, deCX)
			band_extend[i * h:(i + 1) * h, j * w:(j + 1) * w] = _decode_block(decodeD, deCX, h, w, num)
		if codestream[0] != 2049:
			print("Error!")
		codestream = codestream[1:]
	if codestream[0] != 2050:
		print("Error!")
	# print("finish one band")
	return band_extend[0:h_cA, 0:w_cA]


def _decode_block(D, CX, h=64, w=64, num=32):
	deS1 = np.uint32(np.zeros((h, w)))
	deS2 = np.uint32(np.zeros((h, w)))
	deS3 = np.uint32(np.zeros((h, w)))
	signs = np.uint32(np.zeros((h, w)))
	V = np.uint32(np.zeros((num, h, w)))
	deCode = np.zeros((h, w))
	pointer = 0
	for i in range(num):
		V[i, :, :], signs, deS1, deS3, pointer = _SignificancePassDecoding(V[i, :, :], D, CX, deS1, deS3, pointer, signs, w,
																																			 h)
		V[i, :, :], deS2, pointer = _MagnitudePassDecoding(V[i, :, :], D, deS1, deS2, deS3, pointer, w, h)
		V[i, :, :], deS1, deS3, signs, pointer = _CleanPassDecoding(V[i, :, :], D, CX, deS1, deS3, pointer, signs, w, h)
		deS3 = np.zeros((h, w))
	V = np.transpose(V, (1, 2, 0))
	tempV = np.zeros((h,w))
	for i in range(h):
		for j in range(w):
			tempV[i][j] = sum([V[i][j][k]*2**(num-1-k) for k in range(num)])

	for i in range(h):
		for j in range(w):
			deCode[i][j] = (1 - 2 * signs[i][j]) * tempV[i][j]
	return deCode


def _SignificancePassDecoding(V, D, CX, deS1, deS3, pointer, signs, w=64, h=64):
	S1extend = np.pad(deS1, ((1, 1), (1, 1)), 'constant')
	rounds = h // 4
	for i in range(rounds):
		for col in range(w):
			for ii in range(4):
				row = 4 * i + ii
				temp = np.sum(S1extend[row:row + 3, col:col + 3]) - S1extend[row + 1][col + 1]
				if deS1[row][col] != 0 or temp == 0:
					continue
				###
				if pointer>=len(D):
					continue
				V[row][col] = D[pointer][0]
				pointer = pointer + 1
				deS3[row][col] = 1
				if V[row][col] == 1:
					signs[row][col] = _SignDecoding(D[pointer], CX[pointer], S1extend[row:row + 3, col:col + 3])
					pointer = pointer + 1
					deS1[row][col] = 1
					S1extend = np.pad(deS1, ((1, 1), (1, 1)), 'constant')
	return V, signs, deS1, deS3, pointer


def _MagnitudePassDecoding(V, D, deS1, deS2, deS3, pointer, w=64, h=64):
	rounds = h // 4
	for i in range(rounds):
		for col in range(w):
			for ii in range(4):
				row = 4 * i + ii
				if deS1[row][col] != 1 or deS3[row][col] != 0:
					continue
				###
				if pointer>=len(D):
					continue
				V[row][col] = D[pointer][0]
				pointer = pointer + 1
				deS2[row][col] = 1
	return V, deS2, pointer


def _CleanPassDecoding(V, D, CX, deS1, deS3, pointer, signs, w=64, h=64):
	a = pointer
	S1extend = np.pad(deS1, ((1, 1), (1, 1)), 'constant')
	rounds = h // 4
	for i in range(rounds):
		for col in range(w):
			ii = 0
			row = 4 * i
			tempSum = np.sum(S1extend[row:row + 6, col:col + 3]) + np.sum(deS3[row:row + 4, col])
			# 整一列未被编码，都为非重要，且领域非重要
			if tempSum == 0:
				if CX.__len__() < pointer + 3:
					CXextend = np.pad(CX, (0, 2), 'constant')
					Dextend = np.pad(D, (0, 2), 'constant')
					tempCx = CXextend[pointer:pointer + 3]
					tempD = Dextend[pointer:pointer + 3]
				else:
					tempCx = CX[pointer:pointer + 3]
					tempD = D[pointer:pointer + 3]
				ii, tempV = _RunLengthDecoding(tempCx, tempD)
				if tempV == [0, 0, 0, 0]:
					V[row][col] = 0
					V[row + 1][col] = 0
					V[row + 2][col] = 0
					V[row + 3][col] = 0
					pointer = pointer + 1
				else:
					if tempV == [1]:
						V[row][col] = 1
						pointer = pointer + 3
					elif tempV == [0, 1]:
						V[row][col] = 0
						V[row + 1][col] = 1
						pointer = pointer + 3
					elif tempV == [0, 0, 1]:
						V[row][col] = 0
						V[row + 1][col] = 0
						V[row + 2][col] = 1
						pointer = pointer + 3
					elif tempV == [0, 0, 0, 1]:
						V[row][col] = 0
						V[row + 1][col] = 0
						V[row + 2][col] = 0
						V[row + 3][col] = 1
						pointer = pointer + 3
					# sign coding
					row = row + ii - 1
					###
					if pointer>=len(D):
						continue
					signs[row][col] = _SignDecoding(D[pointer], CX[pointer], S1extend[row:row + 3, col:col + 3])
					pointer = pointer + 1
					deS1[row][col] = 1
					S1extend = np.pad(deS1, ((1, 1), (1, 1)), 'constant')
			while ii < 4:
				row = i * 4 + ii
				ii = ii + 1
				if deS1[row][col] != 0 or deS3[row][col] != 0:
					continue
				###
				if pointer>=len(D):
					continue
				V[row][col] = D[pointer][0]
				pointer = pointer + 1
				deS3[row][col] = 1
				if V[row][col] == 1:
					signs[row][col] = _SignDecoding(D[pointer], CX[pointer], S1extend[row:row + 3, col:col + 3])
					pointer = pointer + 1
					deS1[row][col] = 1
					S1extend = np.pad(deS1, ((1, 1), (1, 1)), 'constant')
	return V, deS1, deS3, signs, pointer


def _RunLengthDecoding(CX, D):
	n = CX.__len__()
	wrong = 1
	if CX[0][0] == 17 and D[0][0] == 0 or CX[0][0] == 17 and CX[1][0] == 18 and CX[2][0] == 18 and D[0][0] == 1:
		wrong = 0
	if wrong == 0:
		if D[0][0] == 0:
			deLen = 4
			V = [0, 0, 0, 0]
		elif D[0][0] == 1 and D[1][0] == 0 and D[2][0] == 0:
			deLen = 1
			V = [1]
		elif D[0][0] == 1 and D[1][0] == 0 and D[2][0] == 1:
			deLen = 2
			V = [0, 1]
		elif D[0][0] == 1 and D[1][0] == 1 and D[2][0] == 0:
			deLen = 3
			V = [0, 0, 1]
		elif D[0][0] == 1 and D[1][0] == 1 and D[2][0] == 1:
			deLen = 4
			V = [0, 0, 0, 1]
		else:
			# self.logs[-1] += self.formatter.warning('_RunLengthDecoding: D not valid')
			deLen = -1
			V = [-1]
			"""
			try:
				raise ValidationError('_RunLengthDecoding: D not valid')
			except ValidationError as e:
				print(e.args)
				deLen = -1
				V = [-1]
			"""
	else:
		# self.logs[-1] += self.formatter.warning('_RunLengthDecoding: CX not valid')
		deLen = -1
		V = [-1]
		"""
		try:
			raise ValidationError('_RunLengthDecoding: CX not valid')
		except ValidationError as e:
			print(e.args)
			deLen = -1
			V = [-1]
		"""
	return deLen, V


def _SignDecoding(D, CX, neighbourS1):
	if neighbourS1.__len__() == 3 and neighbourS1[0].__len__() == 3:
		hstr = str(int(neighbourS1[1][0])) + str(int(neighbourS1[1][2]))
		vstr = str(int(neighbourS1[0][1])) + str(int(neighbourS1[2][1]))
		dict = {
			'00': 0, '1-1': 0, '-11': 0, '01': 1, '10': 1, '11': 1,
			'0-1': -1, '-10': -1, '-1-1': -1
		}
		h = dict[hstr]
		v = dict[vstr]
		hAndv = str(h) + str(v)
		hv2Sign = {
			'11': 0, '10': 0, '1-1': 0, '01': 0, '00': 0,
			'0-1': 1, '-11': 1, '-10': 1, '-1-1': 1
		}
		hv2Context = {
			'11': 13, '10': 12, '1-1': 11, '01': 10, '00': 9,
			'0-1': 10, '-11': 11, '-10': 12, '-1-1': 13
		}
		temp = hv2Sign[hAndv]
		deCX = hv2Context[hAndv]
		if deCX == CX:
			deSign = D[0] ^ temp
		else:
			# self.logs[-1] += self.formatter.warning('_SignDecoding: Context does not match. Error occurs.')
			deSign = -1
			"""
			try:
				raise ValidationError('_SignDecoding: Context does not match. Error occurs.')
			except ValidationError as e:
				print(e.args)
				deSign = -1
			"""
	else:
		# self.logs[-1] += self.formatter.warning('_SignDecoding: Size of neighbourS1 not valid')
		deSign = -1
		"""
		try:
			raise ValidationError('_SignDecoding: Size of neighbourS1 not valid')
		except ValidationError as e:
			print(e.args)
			deSign = -1
		"""
	return deSign


class EBCOTparam(object):
	"""
	EBCOT parameter is parameter used by MQ encode and decode processes
	
	initialized in MQ encoding
	interval length         A = 8000H    
	Lower bound register        C = 0         
	Current code byte number    L = -1
	Temporary byte buffer     T = 0
	Down-counter          t = 12

	"""

	def __init__(self):
		self.C = np.uint32(0)
		self.A = np.uint16(32768)
		self.t = np.uint8(12)
		self.T = np.uint8(0)
		self.L = np.int32(-1)
		self.stream = np.uint8([])



'''
改了decodeblock，_embeddedBlockEncoder，banddecode和bandencode改了num的预设值
'''

# if __name__ == "__main__":
# 	h, w = 64, 64
# 	testblock = np.zeros((h, w), dtype=np.uint8)
# 	for i in range(h):
# 		for j in range(w):
# 			testblock[i][j] = i*4
# 	#(bitcode, _) = _band_encode(testblock, "LL", h=64, w=64, num=8)
# 	#decodeblock = _band_decode(list(bitcode), h=64, w=64, num=32)
# 	CX, D, bitplanelength = _embeddedBlockEncoder(testblock, "LL", h, w, num=8)
# 	encoder = _MQencode(CX, D)
# 	decodeD = _MQ_decode(encoder.stream, CX)
# 	decodeblock = _decode_block(D, CX, h, w, num=8)


# 	a = 1
