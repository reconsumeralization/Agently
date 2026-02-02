from datetime import datetime


class TimeInfo:
    @staticmethod
    def get_current_time():
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")
