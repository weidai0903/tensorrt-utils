#!/usr/bin/env python3

# Copyright 2019 NVIDIA Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import sys
import glob
import math
import logging
import argparse

import tensorrt as trt

from ImagenetCalibrator import ImagenetCalibrator, get_calibration_files # local module

TRT_LOGGER = trt.Logger()
logging.basicConfig(level=logging.DEBUG,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

def get_batch_sizes(max_batch_size):
    max_exponent = int(math.log2(max_batch_size))
    # Returns powers of 2, up to and including max_batch_size
    batch_sizes = set([2**i for i in range(max_exponent+1)])
    batch_sizes.add(max_batch_size)
    return batch_sizes


def create_optimization_profile(builder, config, input_name, input_shape, batch_size=None):
    profile = builder.create_optimization_profile()

    # Fixed explicit batch in input shape
    if batch_size is None:
        batch_size = input_shape[0]
        shape = input_shape[1:]
    # Dynamic explicit batch
    elif input_shape[0] == -1:
        shape = input_shape[1:]
    # Implicit Batch
    else:
        shape = input_shape

    min_batch = batch_size
    opt_batch = batch_size
    max_batch = batch_size
    logger.info("Optimization profile: Min{}, Opt{}, Max{}".format(
                   (min_batch, *shape), (opt_batch, *shape), (max_batch, *shape),
               ))
    profile.set_shape(input_name, min=(min_batch, *shape), opt=(opt_batch, *shape), max=(max_batch, *shape))
    config.add_optimization_profile(profile)


def main():
    parser = argparse.ArgumentParser(description="Creates a TensorRT engine from the provided ONNX file.\n")
    parser.add_argument("--onnx", required=True, help="The ONNX model file to convert to TensorRT")
    parser.add_argument("-o", "--output", type=str, default="model.engine", help="The path at which to write the engine")
    parser.add_argument("-b", "--max-batch-size", type=int, default=32, help="The max batch size for the TensorRT engine input")
    parser.add_argument("-v", "--verbosity", action="count", help="Verbosity for logging. (None) for ERROR, (-v) for INFO/WARNING/ERROR, (-vv) for VERBOSE.")
    parser.add_argument("--explicit-batch", action='store_true', help="Set trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH flag.")
    parser.add_argument("--fp16", action="store_true", help="Attempt to use FP16 kernels when possible.")
    parser.add_argument("--int8", action="store_true", help="Attempt to use INT8 kernels when possible. This should generally be used in addition to the --fp16 flag. \
                                                             ONLY SUPPORTS RESNET-LIKE MODELS SUCH AS RESNET50/VGG16/INCEPTION/etc.")
    parser.add_argument("--calibration-cache", help="(INT8 ONLY) The path to read/write from calibration cache.", default="calibration.cache")
    parser.add_argument("--calibration-data", help="(INT8 ONLY) The directory containing {*.jpg, *.jpeg, *.png} files to use for calibration. (ex: Imagenet Validation Set)", default=None)
    parser.add_argument("--calibration-batch-size", help="(INT8 ONLY) The batch size to use during calibration.", type=int, default=32)
    parser.add_argument("--max-calibration-size", help="(INT8 ONLY) The max number of data to calibrate on from --calibration-data.", type=int, default=512)
    parser.add_argument("-p", "--preprocess_func", type=str, default=None, help="(INT8 ONLY) Function defined in 'processing.py' to use for pre-processing calibration data.")
    args, _ = parser.parse_known_args()

    # Adjust logging verbosity
    if args.verbosity is None:
        TRT_LOGGER.min_severity = trt.Logger.Severity.ERROR
    # -v
    elif args.verbosity == 1:
        TRT_LOGGER.min_severity = trt.Logger.Severity.INFO
    # -vv
    else:
        TRT_LOGGER.min_severity = trt.Logger.Severity.VERBOSE
    logger.info("TRT_LOGGER Verbosity: {:}".format(TRT_LOGGER.min_severity))

    # Network flags / Explicit Batch
    network_flags = 0
    if args.explicit_batch:
        network_flags |= 1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH)

    # Building engine
    with trt.Builder(TRT_LOGGER) as builder, \
         builder.create_network(network_flags) as network, \
         builder.create_builder_config() as config, \
         trt.OnnxParser(network, TRT_LOGGER) as parser:
            
        builder.max_batch_size = args.max_batch_size
        config.max_workspace_size = 2**30 # 1GiB

        if args.fp16:
            logger.info("Using FP16 build flag")
            config.set_flag(trt.BuilderFlag.FP16)

        if args.int8:
            logger.info("Using INT8 build flag")
            config.set_flag(trt.BuilderFlag.INT8)

            # Use calibration cache if it exists
            if os.path.exists(args.calibration_cache):
                logger.info("Skipping calibration files, using calibration cache: {:}".format(args.calibration_cache))
                calibration_files = []
            # Use calibration files from validation dataset if no cache exists
            else:
                if not args.calibration_data:
                    raise ValueError("ERROR: Int8 mode requested, but no calibration data provided. Please provide --calibration-data /path/to/calibration/files")

                calibration_files = get_calibration_files(args.calibration_data, args.max_calibration_size)

            # Choose pre-processing function for INT8 calibration
            import processing
            if args.preprocess_func is not None:
                preprocess_func = getattr(processing, args.preprocess_func)
            else:
                preprocess_func = processing.preprocess_imagenet

            config.int8_calibrator = ImagenetCalibrator(calibration_files=calibration_files, batch_size=args.calibration_batch_size,
                                                        cache_file=args.calibration_cache, preprocess_func=preprocess_func)

        # Fill network atrributes with information by parsing model
        with open(args.onnx, "rb") as f:
            if not parser.parse(f.read()):
                print('ERROR: Failed to parse the ONNX file: {}'.format(args.onnx))
                for error in range(parser.num_errors):
                    print(parser.get_error(error))
                sys.exit(1)

        # Mark output if not already marked
        if not network.get_output(0):
            logger.info("No output layer found, marking last layer as output. Correct this if wrong.")
            network.mark_output(network.get_layer(network.num_layers-1).get_output(0))

        if args.explicit_batch:
            input_shape = network.get_input(0).shape
            logger.debug("network.get_input(0).shape = {:}".format(input_shape))
            input_name = network.get_input(0).name
            logger.debug("network.get_input(0).name = {:}".format(input_name))

            # Dynamic shape, create multiple optimization profiles
            if input_shape[0] == -1:
                logger.info("Explicit batch size is dynamic (-1), creating several optimization profiles...")
                for batch_size in sorted(get_batch_sizes(args.max_batch_size)):
                    create_optimization_profile(builder, config, input_name, input_shape, batch_size)
            # Fixed shape, create profile with wide range of batch sizes for convenience
            else:
                logger.info("Explicit batch size is fixed ({}), creating one optimization profile...".format(input_shape[0]))
                create_optimization_profile(builder, config, input_name, input_shape, None)

        logger.info("Building Engine...")
        with builder.build_engine(network, config) as engine, open(args.output, "wb") as f:
            logger.info("Writing engine to {:}".format(args.output))
            f.write(engine.serialize())

if __name__ == "__main__":
    main()
