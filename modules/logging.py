class Log:
    @staticmethod
    def i(message):
        print(f"\033[94m=>\033[0m {message}")

    @staticmethod
    def s(message):
        print(f"\033[92m✓\033[0m {message}")

    @staticmethod
    def e(message):
        print(f"\033[91m✗\033[0m {message}")

    @staticmethod
    def w(message):
        print(f"\033[93m!\033[0m {message}")
