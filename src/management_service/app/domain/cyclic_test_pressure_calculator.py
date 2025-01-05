class CyclicTestPressureCalculator:
    HIGH_PRESSURE_FACTORS = [1.0,0.8,0.6,0.5]
    LOW_PRESSURE_FACTORS = [0.3,0.5,0.0,0.2]
    CYCLE_COUNT = [3500, 300, 600, 100, 50, 1050, 50, 3050]    

    @staticmethod
    def _get_high_pressure(design_load, index):
        return design_load * CyclicTestPressureCalculator.HIGH_PRESSURE_FACTORS[abs(index - 4) - 1]

    @staticmethod
    def _get_low_pressure(design_load, index):
        return design_load * CyclicTestPressureCalculator.LOW_PRESSURE_FACTORS[abs(index - 4) - 1]
    
    @staticmethod
    def _get_cycle_count(index):
        return CyclicTestPressureCalculator.CYCLE_COUNT[index]

    @staticmethod
    def get_cylcic_test_data(design_load, index):
        return CyclicTestPressureCalculator._get_high_pressure(design_load, index), \
               CyclicTestPressureCalculator._get_low_pressure(design_load, index),\
               CyclicTestPressureCalculator._get_cycle_count(index)
                   

