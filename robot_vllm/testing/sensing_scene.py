"""Small MuJoCo visual target-reaching benchmark, not a grasping benchmark.

Four independently actuated sliders share a top camera. The model sees rendered
pixels and public grid calibration only. Target positions are used only by the
post-execution adjudicator, never by perception or the action decoder.
"""
import base64
import io


COLORS = ("red", "green", "blue", "yellow")
X = (-.6, -.3, 0, .3, .6)
Y = (.6, .2, -.2, -.6)


class SensingScene:
    def __init__(self, seed):
        import mujoco
        import numpy as np
        self.mj = mujoco
        self.rng = np.random.default_rng(seed)
        rgba = (".9 .08 .08 1", ".05 .7 .12 1", ".05 .2 .95 1", ".95 .8 .03 1")
        bodies, actuators = [], []
        for i, y in enumerate(Y):
            bodies.append(f'<body name="robot{i}" pos="0 {y} .06"><joint name="slide{i}" type="slide" axis="1 0 0" range="-.78 .78" damping="2"/><geom type="sphere" size=".027" rgba=".08 .08 .08 1" mass=".1" contype="0" conaffinity="0"/></body>')
            bodies.append(f'<body name="target{i}" pos="0 {y} .035"><geom type="cylinder" size=".075 .02" rgba="{rgba[i]}" contype="0" conaffinity="0"/></body>')
            actuators.append(f'<position joint="slide{i}" kp="45" kv="4" ctrlrange="-.78 .78"/>')
        self.xml = ('<mujoco model="shared_sensing_target_reaching"><option timestep=".005" gravity="0 0 0" integrator="implicitfast"/>'
                    '<visual><global offwidth="512" offheight="512"/><quality shadowsize="1024"/></visual><worldbody>'
                    '<light pos="0 0 3" diffuse="1 1 1" ambient=".5 .5 .5"/><camera name="top" pos="0 0 3" fovy="33.398488"/>'
                    '<geom type="plane" size="1 1 .01" rgba=".9 .9 .9 1"/>' + ''.join(bodies) + '</worldbody><actuator>' + ''.join(actuators) + '</actuator></mujoco>')
        self.model = mujoco.MjModel.from_xml_string(self.xml)
        self.data = mujoco.MjData(self.model)
        self.data.qpos[:] = -.74
        self.data.ctrl[:] = -.74
        self.renderer = mujoco.Renderer(self.model, 512, 512)
        self.revision = 0
        self._targets = None
        self.change_targets()

    def change_targets(self):
        import numpy as np
        previous = self._targets
        targets = self.rng.integers(0, 5, size=4)
        if previous is not None:
            targets = np.array([(int(old) + int(self.rng.integers(1, 5))) % 5 for old in previous])
        self._targets = targets
        for i, cell in enumerate(targets):
            self.model.body_pos[self.mj.mj_name2id(self.model, self.mj.mjtObj.mjOBJ_BODY, f"target{i}"), 0] = X[cell]
        self.revision += 1
        self.mj.mj_forward(self.model, self.data)

    def camera(self, path):
        from PIL import Image, ImageDraw
        self.renderer.update_scene(self.data, camera="top")
        image = Image.fromarray(self.renderer.render())
        draw = ImageDraw.Draw(image)
        for cell, x in enumerate(X, 1):
            u = int((x / 1.8 + .5) * 512)
            draw.text((u - 4, 6), str(cell), fill="black", stroke_width=1)
            draw.line((u, 25, u, 510), fill=(160, 160, 160), width=1)
        image.save(path)
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        return {"mime_type": "image/png", "encoding": "base64", "data": base64.b64encode(stream.getvalue()).decode(),
                "width": 512, "height": 512, "source": "mujoco_top_camera"}

    def execute(self, predictions):
        """Map model-selected public grid columns to actuator setpoints."""
        for i, color in enumerate(COLORS):
            self.data.ctrl[i] = X[predictions[color] - 1]
        trace = []
        for step in range(160):
            self.mj.mj_step(self.model, self.data)
            if step % 10 == 0:
                trace.append({"sim_time": float(self.data.time), "measured_qpos": self.data.qpos.tolist()})
        return trace

    def final_verdict(self):
        errors = [abs(float(self.data.qpos[i]) - X[cell]) for i, cell in enumerate(self._targets)]
        return {"success": all(e < .03 for e in errors), "per_robot_success": [e < .03 for e in errors],
                "max_position_error_m": max(errors), "source": "posthoc_mujoco_qpos_vs_target"}

    def close(self):
        self.renderer.close()
