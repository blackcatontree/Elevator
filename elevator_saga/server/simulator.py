#!/usr/bin/env python3
"""
智能电梯调度算法 (Look-Ahead/SCAN策略)
目标: 最小化乘客等待时间，通过沿途接送和合理任务分配实现高效运行。
"""
from typing import Dict, List, Set, Tuple

from elevator_saga.client.base_controller import ElevatorController
from elevator_saga.client.proxy_models import ProxyElevator, ProxyFloor, ProxyPassenger
from elevator_saga.core.models import SimulationEvent, Direction


class LookAheadDispatcher(ElevatorController):
    """
    Look-Ahead 策略的电梯调度器
    """

    def __init__(self, server_url: str = "http://127.0.0.1:8000", debug: bool = True):
        super().__init__(server_url, debug)
        self.elevators: List[ProxyElevator] = []
        self.floors: List[ProxyFloor] = []
        # 全局待处理呼叫队列：{楼层编号：{'up'/'down': [ProxyPassenger, ...]}}
        self.unassigned_calls: Dict[int, Dict[str, List[ProxyPassenger]]] = {}
        # 记录每个电梯当前的运行方向（仅用于空闲时的偏好）
        self.elevator_current_directions: Dict[int, str] = {}
        self.max_floor = 0 
        self.current_tick = 0

    def _initialize_calls_queue(self) -> None:
        """初始化呼叫队列结构"""
        for floor in self.floors:
            self.unassigned_calls[floor.floor] = {'up': [], 'down': []}

    def on_init(self, elevators: List[ProxyElevator], floors: List[ProxyFloor]) -> None:
        """初始化电梯环境和策略"""
        print("🤖 智能电梯调度算法初始化")
        print(f"管理 {len(elevators)} 部电梯，服务 {len(floors)} 层楼")
        
        self.elevators = elevators
        self.floors = floors
        self.max_floor = len(floors) - 1
        self._initialize_calls_queue()

        # 初始分配：均匀分散电梯
        for i, elevator in enumerate(elevators):
            # 均匀分布在不同楼层作为起始待命点
            target_floor = (i * self.max_floor) // len(elevators)
            # 设置初始方向（默认向上，直到接到任务）
            self.elevator_current_directions[elevator.id] = "up"
            # 立刻移动到目标位置
            elevator.go_to_floor(target_floor, immediate=True)

    def on_event_execute_start(
        self, tick: int, events: List[SimulationEvent], elevators: List[ProxyElevator], floors: List[ProxyFloor]
    ) -> None:
        """时间执行前的记录与同步"""
        self.current_tick = tick
        pass

    def on_event_execute_end(
        self, tick: int, events: List[SimulationEvent], elevators: List[ProxyElevator], floors: List[ProxyFloor]
    ) -> None:
        """事件执行后的回调"""
        pass

    def _calculate_cost(self, elevator: ProxyElevator, call_floor: int, call_direction: str) -> float:
        """
        计算电梯服务特定呼叫的成本（等待时间估算）
        成本越低越好。

        由于不能访问内部任务列表，这里使用简化的成本模型，更侧重方向一致性和距离。
        """
        # 1. 优先级：电梯是否空闲
        if elevator.is_idle:
            # 成本 = 到达时间 (距离)
            return abs(call_floor - elevator.current_floor)
        
        current_dir = elevator.target_floor_direction.value
        current_pos = elevator.current_floor_float
        
        # 2. 顺路接客（方向一致，且呼叫在电梯前方）
        if current_dir == call_direction:
            is_ahead = (current_dir == Direction.UP.value and call_floor >= current_pos) or \
                       (current_dir == Direction.DOWN.value and call_floor <= current_pos)
            
            if is_ahead:
                # 成本 = 到达呼叫楼层所需的距离/时间
                return abs(current_pos - call_floor)
            
        # 3. 反方向呼叫或在后方呼叫（高成本）
        # 简单估算：完成当前目标 + 折返惩罚 + 呼叫距离
        if elevator.target_floor is not None:
            # 预估成本 = 到达当前目标所需距离 + 调头惩罚 + 从目标楼层到呼叫楼层所需距离
            cost_to_target = abs(current_pos - elevator.target_floor)
            cost_after_turn = abs(elevator.target_floor - call_floor)
            return cost_to_target + 500.0 + cost_after_turn
        
        # 默认高成本
        return 9999.0

    def on_passenger_call(self, passenger:ProxyPassenger, floor: ProxyFloor, direction: str) -> None:
        """
        乘客呼叫时的调度分配
        """
        call_floor_num = floor.floor
        
        # 1. 存储呼叫到全局队列
        self.unassigned_calls[call_floor_num][direction].append(passenger)
        
        best_elevator: ProxyElevator | None = None
        min_cost = float('inf')

        # 2. 寻找最佳电梯
        for elevator in self.elevators:
            cost = self._calculate_cost(elevator, call_floor_num, direction)
            
            # 只有在成本低于当前最优时才考虑
            if cost < min_cost:
                min_cost = cost
                best_elevator = elevator

        # 3. 分配任务
        if best_elevator:
            # 如果电梯是空闲的，立刻启动它
            if best_elevator.is_idle:
                self.on_elevator_idle(best_elevator)
            
            # 如果电梯正在移动，它会在 on_elevator_approaching 中决定是否停靠

    def _find_closest_call_target(self, elevator: ProxyElevator) -> int | None:
        """
        查找当前未分配呼叫中，距离电梯最近的楼层
        """
        min_dist = float('inf')
        closest_floor = None
        current_pos = elevator.current_floor
        
        # 遍历所有楼层的呼叫
        for floor_num in range(self.max_floor + 1):
            if self.unassigned_calls[floor_num]['up'] or self.unassigned_calls[floor_num]['down']:
                dist = abs(floor_num - current_pos)
                if dist < min_dist:
                    min_dist = dist
                    closest_floor = floor_num
                    
        return closest_floor

    def on_elevator_idle(self, elevator: ProxyElevator) -> None:
        """
        空闲电梯任务分配或待命
        """
        # 1. 尝试找到最近的未分配呼叫
        next_call_floor = self._find_closest_call_target(elevator)
        
        if next_call_floor is not None:
            print(f"E{elevator.id} 空闲，分配外部任务到 F{next_call_floor}")
            elevator.go_to_floor(next_call_floor)
            return

        # 2. 无任务，回到待命位置 (例如，回到均匀分散的待命点)
        standby_floor = (elevator.id * self.max_floor) // len(self.elevators)
        if elevator.current_floor != standby_floor:
            print(f"E{elevator.id} 无任务，返回待命点 F{standby_floor}")
            elevator.go_to_floor(standby_floor)

    def on_elevator_stopped(self, elevator: ProxyElevator, floor: ProxyFloor) -> None:
        """
        电梯停靠时的开关门和上下客处理
        """
        floor_num = floor.floor
        
        # 1. 乘客下车和内部任务清理由模拟器自动处理

        # 2. 乘客上车 (根据当前电梯方向决定接客)
        current_dir = elevator.target_floor_direction.value
        
        # 尝试接上行乘客
        if current_dir == Direction.UP.value and self.unassigned_calls[floor_num]['up']:
            passengers_to_board = self.unassigned_calls[floor_num]['up'][:]
            for passenger in passengers_to_board:
                if len(elevator.passengers) < elevator.capacity:
                    elevator.add_passenger_to_board(passenger)
                    self.unassigned_calls[floor_num]['up'].remove(passenger)
        
        # 尝试接下行乘客
        elif current_dir == Direction.DOWN.value and self.unassigned_calls[floor_num]['down']:
            passengers_to_board = self.unassigned_calls[floor_num]['down'][:]
            for passenger in passengers_to_board:
                if len(elevator.passengers) < elevator.capacity:
                    elevator.add_passenger_to_board(passenger)
                    self.unassigned_calls[floor_num]['down'].remove(passenger)
            
        # 3. 决定下一站 (在乘客上下完成后)
        
        # ❗️ 关键修正：由于不能访问 get_mission_floors，我们信任模拟器处理内部任务。

        # 3a. 如果电梯内有乘客：
        #     我们信任模拟器会根据乘客按下的按钮自动前往下一个目标楼层。
        #     我们不调用 go_to_floor() 以避免覆盖模拟器的内部任务。
        if elevator.passengers:
            print(f"E{elevator.id} 载有乘客，让模拟器内部任务驱动。")
            return
            
        # 3b. 如果电梯内没有乘客：
        #     电梯将自动进入 idle 状态，触发 on_elevator_idle 来分配外部呼叫。
        print(f"E{elevator.id} 任务完成，转入空闲等待...")
        # on_elevator_idle 会立即被触发来寻找外部呼叫或返回待命点
        

    # --- 辅助回调函数 ---

    def on_passenger_board(self, elevator: ProxyElevator, passenger: ProxyPassenger) -> None:
        """乘客上梯时的回调"""
        # 乘客目的地将由模拟器在内部任务列表中自动设置
        print(f" E{elevator.id} 乘客 {passenger.id} ⬆️ F{elevator.current_floor} -> F{passenger.destination}")

    def on_passenger_alight(self, elevator: ProxyElevator, passenger: ProxyPassenger, floor: ProxyFloor) -> None:
        """乘客下车时的回调"""
        print(f" E{elevator.id} 乘客 {passenger.id} ⬇️ F{floor.floor}")

    def on_elevator_passing_floor(self, elevator: ProxyElevator, floor: ProxyFloor, direction: str) -> None:
        """
        电梯经过楼层时的回调：动态顺路接客
        """
        pass # 逻辑已转移到 on_elevator_approaching

    def on_elevator_approaching(self, elevator: ProxyElevator, floor: ProxyFloor, direction: str) -> None:
        """
        电梯即将到达时的回调：最后一刻的决策，确保停靠
        """
        floor_num = floor.floor
        
        # ❗️ 关键修正：移除 get_mission_floors 依赖。
        # 1. 内部目的地（乘客下车）：我们信任模拟器会自动处理，不需手动 stop_at_floor。
        
        # 2. 外部目的地（乘客上车/顺路接客）
        # 检查是否有顺路且未被分配的呼叫
        can_board_up = direction == Direction.UP.value and self.unassigned_calls[floor_num].get('up')
        can_board_down = direction == Direction.DOWN.value and self.unassigned_calls[floor_num].get('down')
        
        # 只有在有外部呼叫需要接客时，才强制停靠。
        if can_board_up or can_board_down:
            elevator.stop_at_floor(floor_num)
        else:
            pass

    def on_elevator_move(
        self, elevator: ProxyElevator, from_position: float, to_position: float, direction: str, status: str
    ) -> None:
        """电梯移动时的回调，用于记录"""
        pass 

if __name__ == "__main__":
    algorithm = LookAheadDispatcher()
    algorithm.start()
