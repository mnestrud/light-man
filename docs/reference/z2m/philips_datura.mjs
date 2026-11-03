import * as philips from 'zigbee-herdsman-converters/lib/philips';

// All Philips Hue bulbs in this home, with on_level support added.
// on_level (ZCL genLevelCtrl attribute 0x0011): sets the brightness a bulb
// uses when it receives a ZCL ON command (e.g., from a Zigbee binding).
// Writing this attribute while the bulb is off prestages brightness without
// a state change, eliminating the race between the AL script's state:OFF
// prestage and the binding's ZCL ON command.

export default [
    {
        // Hue white ambiance E26 with Bluetooth (Michael Bedside Light)
        zigbeeModel: ['LTA008'],
        model: '9290022267',
        vendor: 'Philips',
        description: 'Hue white ambiance E26 with Bluetooth',
        extend: [philips.m.light({colorTemp: {range: [153, 434]}, levelConfig: {features: ['on_level']}})],
        ota: true,
    },
    {
        // Hue White and Color Ambiance A19 1100 lumen
        zigbeeModel: ['LCA007'],
        model: '9290024687',
        vendor: 'Philips',
        description: 'Hue White and Color Ambiance A19 1100 lumen',
        extend: [philips.m.light({colorTemp: {range: [153, 500]}, color: true, levelConfig: {features: ['on_level']}})],
        ota: true,
    },
    {
        // Hue white and color ambiance 4" retrofit recessed downlight
        zigbeeModel: ['LCD005'],
        model: '9290031345',
        vendor: 'Philips',
        description: 'Hue white and color ambiance 4" retrofit recessed downlight',
        extend: [philips.m.light({colorTemp: {range: [153, 500]}, color: true, levelConfig: {features: ['on_level']}})],
        ota: true,
    },
    {
        // Hue White and Color Ambiance slim downlight 6"
        zigbeeModel: ['LCD007'],
        model: '579573',
        vendor: 'Philips',
        description: 'Hue White and Color Ambiance slim downlight 6"',
        extend: [philips.m.light({colorTemp: {range: [153, 500]}, color: true, levelConfig: {features: ['on_level']}})],
        ota: true,
    },
    {
        // Philips Hue solo lightstrip (5 meters / 16 feet)
        zigbeeModel: ['LCL009', 'LCL010'],
        model: '929003817002',
        vendor: 'Philips',
        description: 'Philips Hue solo lightstrip (5 meters / 16 feet)',
        extend: [philips.m.light({colorTemp: {range: [153, 500]}, color: {modes: ['xy', 'hs'], enhancedHue: true}, levelConfig: {features: ['on_level']}})],
        ota: true,
    },
    {
        // Hue Lightguide E26 Globe G30 500lm (Kitchen Island)
        zigbeeModel: ['LCO003'],
        model: '929003151601',
        vendor: 'Philips',
        description: 'Hue Lightguide E26 Globe G30 500lm',
        extend: [philips.m.light({colorTemp: {range: [153, 500]}, color: {modes: ['xy', 'hs'], enhancedHue: true}, levelConfig: {features: ['on_level']}})],
        ota: true,
    },
    {
        // Hue Filament White Ambiance A60/E27 Bluetooth (Upstairs Bath)
        zigbeeModel: ['LTA007'],
        model: '9290024783',
        vendor: 'Philips',
        description: 'Hue Filament White Ambiance A60/E27 Bluetooth',
        extend: [philips.m.light({colorTemp: {range: [222, 454]}, levelConfig: {features: ['on_level']}})],
        ota: true,
    },
    {
        // Hue Lightguide E26 Globe Large G40 500lm (Guest Bedroom)
        zigbeeModel: ['LCO006'],
        model: '929003151701',
        vendor: 'Philips',
        description: 'Hue Lightguide E26 Globe Large G40 500lm',
        extend: [philips.m.light({colorTemp: {range: [153, 500]}, color: {modes: ['xy', 'hs'], enhancedHue: true}, levelConfig: {features: ['on_level']}})],
        ota: true,
    },
    {
        // Hue White and Color Ambiance Datura Ceiling Light Square (Office)
        zigbeeModel: ['929003736701_01', '929003736701_02'],
        model: '929003736701',
        vendor: 'Signify Netherlands B.V.',
        description: 'Hue White and Color Ambiance Datura ceiling light',
        extend: [philips.m.light({colorTemp: {range: [153, 500]}, color: {modes: ['xy', 'hs'], enhancedHue: true}, levelConfig: {features: ['on_level']}})],
        ota: true,
    },
];
