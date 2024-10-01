
class StaticTestPressureCalculator:
    STATIC_PRESSURE_FACTOR = [0.75, 1, 1.5]
    
    @staticmethod
    def _get_pressure(design_load, index):
        factor = StaticTestPressureCalculator.STATIC_PRESSURE_FACTOR[abs(index - 3) - 1]
        return  design_load * factor 
    
    @staticmethod
    def get_static_test_data(design_load, index):
        p = StaticTestPressureCalculator._get_pressure(design_load, index)
        return  p, 30