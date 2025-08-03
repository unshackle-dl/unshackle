import jsonpickle


class MSLObject:
    def __repr__(self):
        return "<{} {}>".format(self.__class__.__name__, jsonpickle.encode(self, unpicklable=False))
