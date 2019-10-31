/*
 * Copyright (c) 2019, NVIDIA CORPORATION.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include <cudf/column/column.hpp>
#include <cudf/column/column_factories.hpp>
#include <cudf/column/column_device_view.cuh>
#include <cudf/strings/strings_column_view.hpp>
#include <cudf/strings/string_view.cuh>
#include <cudf/strings/attributes.hpp>
#include <cudf/utilities/error.hpp>
#include <cudf/utilities/type_dispatcher.hpp>
#include <cudf/utilities/traits.hpp>

#include <rmm/thrust_rmm_allocator.h>
#include <thrust/transform.h>
#include <thrust/transform_scan.h>

namespace
{
/**
 * @brief Returns the length for each string as an integer.
 *
 * No checking is done to prevent overflow if the length value
 * does not fit in the output type. The length value is truncated
 * by casting it to an IntegerType.
 */
template <typename UnaryFunction, typename IntegerType>
struct lengths_fn
{
    const cudf::column_device_view d_strings;
    UnaryFunction ufn; // called for each non-null string to return it's length
    // return integer length for each string
    __device__ IntegerType operator()(cudf::size_type idx)
    {
        IntegerType length = 0;
        if( !d_strings.is_null(idx) )
            length = static_cast<IntegerType>(ufn(d_strings.element<cudf::string_view>(idx)));
        return length;
    }
};

// For creating any integer type output column
struct dispatch_lengths_fn
{
    /**
     * @brief Returns a numeric column containing lengths of each string in
     * based on the provided unary function.
     *
     * Any null string will result in a null entry for that row in the output column.
     *
     * @tparam UnaryFunction Device function that returns an integer given a string_view.
     * @param strings Strings instance for this operation.
     * @param ufn Function returns an integer for each string.
     * @param stream Stream to use for any kernels in this function.
     * @param mr Resource for allocating device memory.
     * @return New column with lengths for each string.
     */
    template<typename IntegerType, typename UnaryFunction, std::enable_if_t<std::is_integral<IntegerType>::value>* = nullptr>
    std::unique_ptr<cudf::column> operator()( const cudf::strings_column_view& strings, UnaryFunction& ufn,
                                              rmm::mr::device_memory_resource* mr,
                                              cudaStream_t stream = 0 )
    {
        auto strings_count = strings.size();
        auto execpol = rmm::exec_policy(stream);
        auto strings_column = cudf::column_device_view::create(strings.parent(),stream);
        auto d_column = *strings_column;
        // copy the null mask
        rmm::device_buffer null_mask;
        cudf::size_type null_count = d_column.null_count();
        if( d_column.nullable() )
            null_mask = rmm::device_buffer( d_column.null_mask(),
                                            cudf::bitmask_allocation_size_bytes(strings_count),
                                            stream, mr);
        // create output column of IntegerType
        auto results = std::make_unique<cudf::column>( cudf::data_type{cudf::experimental::type_to_id<IntegerType>()},
            strings_count, rmm::device_buffer(strings_count * sizeof(IntegerType), stream, mr),
            null_mask, null_count);
        auto results_view = results->mutable_view();
        auto d_lengths = results_view.data<IntegerType>();
        // fill in the lengths
        thrust::transform( execpol->on(stream),
            thrust::make_counting_iterator<cudf::size_type>(0),
            thrust::make_counting_iterator<cudf::size_type>(strings_count),
            d_lengths, lengths_fn<UnaryFunction,IntegerType>{d_column,ufn} );
        results->set_null_count(null_count); // reset null count
        return results;
    }

    template<typename IntegerType, typename UnaryFunction, std::enable_if_t<not std::is_integral<IntegerType>::value>* = nullptr>
    std::unique_ptr<cudf::column> operator()( const cudf::strings_column_view&, UnaryFunction&, rmm::mr::device_memory_resource*, cudaStream_t stream = 0 )
    {
        CUDF_FAIL("Output type must be integral type.");
    }
};

} // namespace

namespace cudf
{
namespace strings
{
namespace detail
{

std::unique_ptr<cudf::column> characters_counts( strings_column_view strings,
                                                 data_type output_type = data_type{INT32},
                                                 rmm::mr::device_memory_resource* mr = rmm::mr::get_default_resource(),
                                                 cudaStream_t stream = 0)
{
    auto ufn = [] __device__ (const cudf::string_view& d_str) { return d_str.length(); };
    return cudf::experimental::type_dispatcher( output_type, dispatch_lengths_fn{},
                                                strings, ufn, mr, stream );
}

std::unique_ptr<cudf::column> bytes_counts( strings_column_view strings,
                                            data_type output_type = data_type{INT32},
                                            rmm::mr::device_memory_resource* mr = rmm::mr::get_default_resource(),
                                            cudaStream_t stream = 0)
{
    auto ufn = [] __device__ (const cudf::string_view& d_str) { return d_str.size_bytes(); };
    return cudf::experimental::type_dispatcher( output_type, dispatch_lengths_fn{},
                                                strings, ufn, mr, stream );
}

} // namespace detail


namespace
{

/**
 * @brief Sets the code-point values for each character in the output
 * integer memory for each string in the strings column.
 * For each string, there is a sub-array in d_results with length equal
 * to the number of characters in that string. The function here will
 * write code-point values to that section as pointed to by the
 * corresponding d_offsets value calculated for that string.
 */
struct code_points_fn
{
    const cudf::column_device_view d_strings;
    const cudf::size_type* d_offsets; // offset within d_results to fill with each string's code-point values
    int32_t* d_results; // base integer array output

    __device__ void operator()(cudf::size_type idx)
    {
        if( d_strings.is_null(idx) )
            return;
        auto d_str = d_strings.element<cudf::string_view>(idx);
        auto result = d_results + d_offsets[idx];
        thrust::copy( thrust::seq, d_str.begin(), d_str.end(), result);
    }
};

} // namespace

namespace detail
{
//
std::unique_ptr<cudf::column> code_points( strings_column_view strings,
                                           rmm::mr::device_memory_resource* mr = rmm::mr::get_default_resource(),
                                           cudaStream_t stream = 0)
{
    auto strings_column = column_device_view::create(strings.parent(),stream);
    auto d_column = *strings_column;

    // create offsets vector to account for each string's character length
    rmm::device_vector<size_type> offsets(strings.size()+1);
    size_type* d_offsets = offsets.data().get();
    thrust::transform_inclusive_scan(rmm::exec_policy(stream)->on(stream),
        thrust::make_counting_iterator<size_type>(0),
        thrust::make_counting_iterator<size_type>(strings.size()),
        d_offsets+1,
        [d_column] __device__(size_type idx) {
            size_type length = 0;
            if( !d_column.is_null(idx) )
                length = d_column.element<string_view>(idx).length();
            return length;
        },
        thrust::plus<size_type>());
    CUDA_TRY(cudaMemsetAsync(d_offsets, 0, sizeof(size_type), stream));

    // the total size is the number of characters in the entire column
    size_type num_characters = offsets.back();
    // create output column with no nulls
    auto results = make_numeric_column( data_type{INT32}, num_characters,
                                        mask_state::UNALLOCATED,
                                        stream, mr );
    auto results_view = results->mutable_view();
    // fill column with character code-point values
    auto d_results = results_view.data<int32_t>();
    // now set the ranges from each strings' character values
    thrust::for_each_n(rmm::exec_policy(stream)->on(stream),
        thrust::make_counting_iterator<cudf::size_type>(0), strings.size(),
        code_points_fn{d_column, d_offsets, d_results} );
    //
    results->set_null_count(0);
    return results;
}

} // namespace detail

// APIS
std::unique_ptr<cudf::column> characters_counts( strings_column_view strings,
                                                 data_type output_type,
                                                 rmm::mr::device_memory_resource* mr)
{
    return detail::characters_counts(strings, output_type, mr);
}

std::unique_ptr<cudf::column> bytes_counts( strings_column_view strings,
                                            data_type output_type,
                                            rmm::mr::device_memory_resource* mr)
{
    return detail::bytes_counts( strings, output_type, mr );
}

std::unique_ptr<cudf::column> code_points( strings_column_view strings,
                                           rmm::mr::device_memory_resource* mr )
{
    return detail::code_points(strings,mr);
}

} // namespace strings
} // namespace cudf
