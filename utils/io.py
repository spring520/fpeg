__all__ = [
  "Reader",
  "Writer"
]

import os
import cv2
import pickle
import numpy as np

from ..base import Pipe
from ..config import read_config


config = read_config()
read_dir = config.get("io", "read_dir")
write_dir = config.get("io", "write_dir")
default_filename = config.get("io", "default_filename")


class Reader(Pipe):
  """
  Reader of images.
  """

  def __init__(self,
               name="Image Reader",
               flag="color",
               binary=False):
    """
    Init and set attributes of a image reader.

    Explicit Attributes
    -------------------
    name: str, optional
      Name of the reader.
    flag: str, optional
      Flag used in cv2.imread, must in ["color", "grayscale", "unchanged"]
    """
    super().__init__()

    self.name = name
    self.flag = flag
    self.binary = binary

  def recv(self, path, **params):
    self.logs.append("")
    self.logs[-1] += self.formatter.message("Receiving data.")
    self.received_ = path
    if not self.binary:
      self.logs[-1] += self.formatter.message("Reading '{}' with flag '{}'.".format(path, self.flag))
      if self.flag == "color":
        X = cv2.imread(path, cv2.IMREAD_COLOR)
      elif self.flag == "grayscale":
        X = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
      elif self.flag == "unchanged":
        X = cv2.imread(path, cv2.IMREAD_UNCHANGED)
      else:
        msg = "Invalid flag of {}."
        self.logs[-1] += self.formatter.error(msg)
        raise ValueError(msg)
    else:
      self.logs[-1] += self.formatter.message("Loading binary file.")
      X = pickle.load(open(path, "rb"))
      
    if X is None:
      msg = "File path is invalid."
      self.logs[-1] += self.formatter.error(msg)
      raise ValueError(msg)

    self.sended_ = [X.astype(int)]

    return self


class Writer(Pipe):
  """
  Writer of images.
  """  

  def __init__(self,
               name="Writer",
               path=os.path.join(write_dir, default_filename),
               binary=False):
    """
    Init and set attributes of a image writer.

    Explicit Attributes
    -------------------
    name: str, optional
      Name of the reader.
    """
    super().__init__()

    self.name = name
    self.path = path
    self.binary = binary

  def recv(self, X, **params):
    self.logs.append("")
    self.logs[-1] += self.formatter.message("Receiving data.")
    self.received_ = X

    X[0] = X[0].astype(np.uint8)
    if not self.binary:
      cv2.imwrite(self.path, X[0])
    else:
      self.logs[-1] += self.formatter.message("Writing binary file.")
      pickle.dump(X, open(self.path, "wb"))
    self.sended_ = X

    return self
